"""GIF 编码核心：调色板生成、最近色映射、抖动、延迟量化、LZW 与容器拼装。

本模块只使用标准库 ``math`` 与 ``struct``，离线运行，不使用随机数。

全局契约
========
- 颜色是 ``(r, g, b)`` 三元组，每个分量是 0..255 的整数。越界分量在使用前
  必须钳制到 0..255（例如 256 -> 255、-1 -> 0），否则会写出非法字节或让
  调色板下标溢出。
- 调色板是颜色列表，长度 1..256；生成调色板时 ``max_colors`` 必须落在 2..256。
- 帧是二维像素 ``list[list[tuple[int, int, int]]]``，``frame[row][col]`` 是一个颜色。
- 索引平面是二维整数 ``list[list[int]]``，元素是该像素在调色板中的下标。
- GIF 帧延迟单位是厘秒（1/100 秒）：输入毫秒，输出厘秒，且最小为 2。
- 颜色比较容差为 1：浮点误差导致的取整偏差不得超过 1。
"""

import math
import struct

MAX_COLORS = 256
MIN_DELAY_CS = 2


class GifError(ValueError):
    """gifenc 的输入或编码错误；批量编码失败时整批抛这一个异常。"""


# ---------------------------------------------------------------------------
# 内部校验工具
# ---------------------------------------------------------------------------

def _is_int(value):
    """真正的整数（排除 bool：True/False 不是颜色或延迟）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(value, name):
    if not _is_int(value):
        raise GifError(f"{name} must be an integer")
    return value


def _require_positive_int(value, name):
    if not _is_int(value) or value <= 0:
        raise GifError(f"{name} must be a positive integer")
    return value


def _clamp(value):
    """把一个整数分量钳制到 0..255（越界时钳制，绝不回绕）。"""
    if value < 0:
        return 0
    if value > 255:
        return 255
    return value


def _clamp_color(color):
    """校验并返回钳制后的 ``(r, g, b)``。"""
    if not isinstance(color, (tuple, list)) or len(color) != 3:
        raise GifError(f"color must be an (r, g, b) triple, got {color!r}")
    channels = []
    for value in color:
        if not _is_int(value):
            raise GifError(f"color channel must be an integer, got {value!r}")
        channels.append(_clamp(value))
    return tuple(channels)


def _is_valid_raw_color(color):
    if not isinstance(color, (tuple, list)) or len(color) != 3:
        return False
    return all(_is_int(value) and 0 <= value <= 255 for value in color)


def _normalize_frame(frame, name="frame"):
    """把一帧转成钳制后的颜色平面，同时校验结构。"""
    if not isinstance(frame, (tuple, list)) or len(frame) == 0:
        raise GifError(f"{name} is empty or not a sequence of rows")
    rows = []
    for row in frame:
        if not isinstance(row, (tuple, list)):
            raise GifError(f"{name} contains a row that is not a sequence")
        rows.append([_clamp_color(pixel) for pixel in row])
    return rows


def _normalize_palette(palette):
    if not isinstance(palette, (tuple, list)):
        raise GifError("palette must be a sequence of colors")
    if not palette:
        raise GifError("palette is empty")
    if len(palette) > MAX_COLORS:
        raise GifError("palette must contain at most 256 colors")
    return [_clamp_color(color) for color in palette]


def _nearest_index(color, palette):
    """平方欧氏距离最近的颜色下标；距离相同时取较小下标。"""
    best_index = 0
    best_distance = None
    for index, candidate in enumerate(palette):
        dr = color[0] - candidate[0]
        dg = color[1] - candidate[1]
        db = color[2] - candidate[2]
        distance = dr * dr + dg * dg + db * db
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index, palette[best_index]


def _round_half_up(value):
    return int(math.floor(value + 0.5))


# ---------------------------------------------------------------------------
# 比特流与 GIF 数据子块
# ---------------------------------------------------------------------------

class _BitWriter:
    """LSB first 比特流写入器。"""

    def __init__(self):
        self._data = bytearray()
        self._accumulator = 0
        self._bits = 0

    def write(self, code, width):
        self._accumulator |= code << self._bits
        self._bits += width
        while self._bits >= 8:
            self._data.append(self._accumulator & 0xFF)
            self._accumulator >>= 8
            self._bits -= 8

    def finish(self):
        if self._bits > 0:
            self._data.append(self._accumulator & 0xFF)
            self._accumulator = 0
            self._bits = 0
        return bytes(self._data)


def _pack_subblocks(data):
    """把裸字节流切成 GIF 数据子块，并以 0x00 结束。"""
    chunks = bytearray()
    for start in range(0, len(data), 255):
        chunk = data[start:start + 255]
        chunks.append(len(chunk))
        chunks += chunk
    chunks.append(0)
    return bytes(chunks)


# ---------------------------------------------------------------------------
# 调色板生成
# ---------------------------------------------------------------------------

def _collect_pixels(frames):
    """展平所有帧的有效像素（已钳制）。"""
    if not isinstance(frames, (tuple, list)):
        raise GifError("frames must be a sequence of frames")
    pixels = []
    for frame in frames:
        for row in _normalize_frame(frame):
            pixels.extend(row)
    return pixels


def _box_span(box):
    """返回 (跨度最大的通道下标, 跨度)；单色盒子返回 (None, 0)。"""
    low = [255, 255, 255]
    high = [0, 0, 0]
    for pixel in box:
        for channel in range(3):
            value = pixel[channel]
            if value < low[channel]:
                low[channel] = value
            if value > high[channel]:
                high[channel] = value
    best_channel = None
    best_span = 0
    for channel in range(3):
        span = high[channel] - low[channel]
        if span > best_span:
            best_span = span
            best_channel = channel
    return best_channel, best_span


def _split_box(box, channel):
    """按该通道的像素中位数把盒子一分为二（以像素计数，而非通道数）。"""
    ordered = sorted(box, key=lambda pixel: pixel[channel])
    middle = len(ordered) // 2
    return ordered[:middle], ordered[middle:]


def _box_average(box):
    count = len(box)
    channels = []
    for channel in range(3):
        total = sum(pixel[channel] for pixel in box)
        channels.append(_clamp(_round_half_up(total / count)))
    return tuple(channels)


def median_cut(frames, max_colors):
    """用中位切分（median cut）从若干帧里生成全局调色板。

    参数:
        frames: 二维 RGB 帧的序列（``list[list[list[tuple]]]``）。
        max_colors: 目标颜色数，必须是 2..256 的整数。

    返回:
        ``list[tuple[int, int, int]]``，满足 ``1 <= len(palette) <= max_colors``：

        - 出现过的颜色数不超过 max_colors 时，只返回真实出现过的颜色，
          因此单色图只得到 1 色；
        - 否则返回 max_colors 色。每次挑“像素数最多”的盒子，沿该盒子内跨度
          最大的通道按像素中位数一分为二；划分以像素计数，不是按通道数整除，
          所以永远不会因为 ``max_colors // 3`` 而只剩 1 色；
        - 每个盒子的代表色是盒内像素各通道的平均值（四舍五入），钳制在 0..255。

    异常:
        max_colors 不是 2..256 的整数、frames 为空或颜色不是三元组时抛
        ``GifError``（不允许除零或返回空调色板）。
    """
    if not _is_int(max_colors) or not 2 <= max_colors <= MAX_COLORS:
        raise GifError("max_colors must be an integer in 2..256")

    pixels = _collect_pixels(frames)
    if not pixels:
        raise GifError("frames contain no pixels")

    distinct = set(pixels)
    if len(distinct) <= max_colors:
        # 只返回真实出现过的颜色（单色图只得到 1 色），顺序按首次出现。
        palette = []
        seen = set()
        for pixel in pixels:
            if pixel not in seen:
                seen.add(pixel)
                palette.append(pixel)
        return palette

    boxes = [pixels]
    while len(boxes) < max_colors:
        # 挑“像素数最多”且还能继续切的盒子。
        candidates = []
        for index, box in enumerate(boxes):
            if len(box) < 2:
                continue
            channel, span = _box_span(box)
            if channel is None or span == 0:
                continue
            candidates.append((len(box), index, channel))
        if not candidates:
            break
        candidates.sort(key=lambda item: (-item[0], item[1]))
        _, chosen, channel = candidates[0]
        left, right = _split_box(boxes[chosen], channel)
        boxes[chosen:chosen + 1] = [left, right]

    palette = [_box_average(box) for box in boxes if box]
    return palette[:max_colors]


def map_to_palette(frame, palette):
    """把一帧 RGB 像素映射成调色板索引平面（最近颜色）。

    参数:
        frame: 二维 RGB 帧；每个像素是 ``(r, g, b)``。
        palette: 1..256 色的调色板。

    返回:
        与 frame 同形的 ``list[list[int]]``，元素是最近颜色的下标：

        - 像素分量先钳制到 0..255（256 -> 255、-1 -> 0），绝不允许字节回绕；
        - 距离用平方欧氏距离 ``dr*dr + dg*dg + db*db`` 比较。平方距离与开方后的
          欧氏距离同序，且避免浮点开方误差；不能只比较单个通道或绝对值；
        - 距离相同时取下标较小的颜色。

    异常:
        frame/palette 为空、颜色不是三元组或调色板超过 256 色时抛 ``GifError``。
    """
    colors = _normalize_palette(palette)
    rows = _normalize_frame(frame)
    return [[_nearest_index(pixel, colors)[0] for pixel in row] for row in rows]


def dither(frame, palette, strength):
    """Floyd-Steinberg 误差扩散抖动，消除色带。

    参数:
        frame: 二维 RGB 帧。
        palette: 1..256 色的调色板。
        strength: 0.0..1.0 的扩散强度；1.0 是完整 Floyd-Steinberg，
            0.0 等价于 ``map_to_palette``。

    返回:
        与 frame 同形的 ``list[list[int]]`` 索引平面。

    算法:
        按行优先逐像素取最近颜色，并把“原始颜色 - 选中颜色”的误差乘以
        strength，以 7/16、3/16、5/16、1/16 的权重扩散到右、左下、下、右下的
        邻居。误差必须写进相邻像素的工作缓冲，不能只做逐点量化，否则恒定
        中间色会整片落到同一索引，出现色带。

    “容差 1”指工作缓冲的浮点取整误差不得超过 1。
    强度越界、palette 为空或颜色非法时抛 ``GifError``。
    """
    if isinstance(strength, bool) or not isinstance(strength, (int, float)):
        raise GifError("strength must be a number in 0.0..1.0")
    strength = float(strength)
    if not math.isfinite(strength) or not 0.0 <= strength <= 1.0:
        raise GifError("strength must be a number in 0.0..1.0")

    colors = _normalize_palette(palette)
    rows = _normalize_frame(frame)
    height = len(rows)
    width = max(len(row) for row in rows)

    # 工作缓冲：用浮点保存每个像素的当前颜色，邻居可以是矩形之外的位置。
    work = [[[float(value) for value in pixel] for pixel in row] for row in rows]
    plane = [[0] * len(row) for row in rows]

    for row in range(height):
        for col in range(len(rows[row])):
            current = tuple(work[row][col])
            # 四舍五入到整数后仍钳制在 0..255，保证取色合法且误差不超过 1。
            rounded = tuple(
                _clamp(_round_half_up(value)) for value in current
            )
            index, chosen = _nearest_index(rounded, colors)
            plane[row][col] = index
            if strength == 0.0:
                continue
            error = [
                (current[channel] - chosen[channel]) * strength
                for channel in range(3)
            ]

            def spread(target_row, target_col, weight):
                if 0 <= target_row < height:
                    target = work[target_row]
                    if 0 <= target_col < len(target):
                        for channel in range(3):
                            target[target_col][channel] += error[channel] * weight

            spread(row, col + 1, 7.0 / 16.0)
            spread(row + 1, col - 1, 3.0 / 16.0)
            spread(row + 1, col, 5.0 / 16.0)
            spread(row + 1, col + 1, 1.0 / 16.0)

    return plane


def quantize_delay(durations_ms):
    """把毫秒帧延迟序列换算成 GIF 的厘秒延迟。

    参数:
        durations_ms: 每帧一个正整数毫秒值。

    返回:
        ``list[int]``，与输入等长；每个元素为 ``max(2, ceil(ms / 10))``。
        GIF 延时的单位是 1/100 秒，直接整除会让 1 ms 变成 0，播放器会把 0
        当作“尽可能快”从而加速播放；因此下限固定为 2 厘秒。

    异常:
        任一值不是正整数（0、负数、浮点、布尔等）时抛 ``GifError``。
    """
    if not isinstance(durations_ms, (tuple, list)):
        raise GifError("durations_ms must be a sequence of positive integers")
    delays = []
    for value in durations_ms:
        if not _is_int(value) or value <= 0:
            raise GifError(
                f"duration must be a positive integer number of milliseconds, got {value!r}"
            )
        delays.append(max(MIN_DELAY_CS, -(-value // 10)))
    return delays


def lzw_encode(indices):
    """按 GIF 规范做 LZW 编码（含数据子块封装）。

    参数:
        indices: 一维非负整数序列，元素范围 0..255（来自索引平面，按行优先展平）。

    返回:
        ``bytes``：第 1 字节是 LZW 最小码宽 min_code_size（2..8，由最大下标
        决定，至少为 2）；随后是若干 GIF 数据子块（1 字节块长 + 最多 255 字节
        数据），最后以 0x00 结束。

    编码规则:
        - clear code = ``1 << min_code_size``，end code = clear + 1；
        - 码流先写 clear，实际码宽从 ``min_code_size + 1`` 位开始（最小 3 位，
          而“最小码宽”字段从 2 起），位序为 LSB first；
        - 每当新增一个字典项、下一个可用码达到 ``2 ** 码宽`` 时码宽加 1；
        - 字典达到 4096 项（12 位）时必须立即输出 clear code 并重置字典与
          码宽，绝不能写出超过 12 位的码，也不能让未重置的字典把编码撑爆；
        - 结尾输出 end code，并补齐最后一个字节。

    异常:
        indices 含非整数或超出 0..255 时抛 ``GifError``。
    """
    try:
        iterator = iter(indices)
    except TypeError:
        raise GifError("indices must be an iterable of integers in 0..255")

    values = []
    for value in iterator:
        if not _is_int(value) or not 0 <= value <= 255:
            raise GifError(
                f"index must be an integer in 0..255, got {value!r}"
            )
        values.append(value)
    if not values:
        raise GifError("indices must not be empty")

    # 最小码宽由最大下标决定，GIF 规定最小为 2 位。
    min_code_size = max(2, max(values).bit_length())
    min_code_size = min(min_code_size, 8)
    clear = 1 << min_code_size
    end = clear + 1

    writer = _BitWriter()
    writer.write(clear, min_code_size + 1)

    def fresh_table():
        return {bytes([value]): value for value in range(clear)}

    table = fresh_table()
    next_code = end + 1
    width = min_code_size + 1

    current = bytes([values[0]])
    for value in values[1:]:
        candidate = current + bytes([value])
        if candidate in table:
            current = candidate
            continue

        writer.write(table[current], width)
        if next_code < 4096:
            table[candidate] = next_code
            next_code += 1
            # 编码端的字典比解码端领先一项，因此当下一个可用码超过 2**码宽
            # 时加宽，才能与解码端“等于 2**码宽即加宽”保持同步。
            if next_code > (1 << width) and width < 12:
                width += 1
        else:
            # 字典已满（12 位），立即输出 clear 并重置，绝不出 13 位码。
            writer.write(clear, width)
            table = fresh_table()
            next_code = end + 1
            width = min_code_size + 1
        current = bytes([value])

    writer.write(table[current], width)
    writer.write(end, width)

    return bytes([min_code_size]) + _pack_subblocks(writer.finish())


def assemble(planes, palette, width, height, loop=0):
    """把若干索引平面拼装成最小合法 GIF89a 字节串。

    参数:
        planes: 序列，元素为 ``(indices, delay_cs)`` 二元组；indices 是二维索引
            平面（``map_to_palette``/``dither`` 的结果），delay_cs 是该帧的厘秒
            延迟，必须是 >= 2 的整数。
        palette: 全局调色板，1..256 色。
        width, height: 画布尺寸，正整数；每个平面都必须恰好是 height 行 width 列。

    返回:
        ``bytes``，结构为：

        - ``b"GIF89a"`` 文件头；
        - 逻辑屏幕描述符（宽高、全局颜色表标志与大小）；
        - 补零到 2 的幂的全局颜色表（至少 2 项，最多 256 项）；
        - NETSCAPE2.0 应用扩展：``21 FF 0B "NETSCAPE2.0" 03 01 00 00 00``，
          循环次数 0 表示无限循环（缺少它播放器只播一次）；
        - 每帧：图形控制扩展 ``21 F9 04 00 <delay_lo> <delay_hi> 00 00``
          （disposal=0、无透明色），图像描述符 ``2C`` 与 LZW 数据；
        - 结束符 0x3B。

    异常:
        调色板/尺寸/平面不合法、索引超过调色板、delay_cs < 2 时抛 ``GifError``。
    """
    _require_positive_int(width, "width")
    _require_positive_int(height, "height")
    if not _is_int(loop) or loop < 0 or loop > 0xFFFF:
        raise GifError("loop must be an integer in 0..65535")
    colors = _normalize_palette(palette)
    if not isinstance(planes, (tuple, list)) or not planes:
        raise GifError("planes must be a non-empty sequence")

    frames = []
    for entry in planes:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise GifError("each plane entry must be an (indices, delay_cs) pair")
        indices, delay_cs = entry
        if not _is_int(delay_cs) or delay_cs < MIN_DELAY_CS:
            raise GifError(f"delay_cs must be an integer >= {MIN_DELAY_CS}")
        if not isinstance(indices, (tuple, list)) or len(indices) != height:
            raise GifError(
                f"plane must have exactly {height} rows, got "
                f"{len(indices) if isinstance(indices, (tuple, list)) else 'non-sequence'}"
            )
        rows = []
        for row in indices:
            if not isinstance(row, (tuple, list)) or len(row) != width:
                raise GifError(f"plane rows must have exactly {width} columns")
            checked = []
            for value in row:
                if not _is_int(value) or not 0 <= value < len(colors):
                    raise GifError(
                        f"palette index must be an integer in 0..{len(colors) - 1}"
                    )
                checked.append(value)
            rows.append(checked)
        frames.append((rows, delay_cs))

    # 全局颜色表补零到 2 的幂（至少 2 项，至多 256 项）。
    table_size = 2
    while table_size < len(colors):
        table_size *= 2
    size_field = table_size.bit_length() - 2  # 2 -> 0, 256 -> 7
    packed = 0x80 | 0x70 | size_field

    out = bytearray(b"GIF89a")
    out += struct.pack("<HHBBB", width, height, packed, 0, 0)
    for index in range(table_size):
        color = colors[index] if index < len(colors) else (0, 0, 0)
        out += bytes(color)

    # NETSCAPE2.0 应用扩展：循环次数 0 表示无限循环。
    out += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01" + struct.pack("<H", loop) + b"\x00"

    for rows, delay_cs in frames:
        out += b"\x21\xF9\x04\x00"
        out += struct.pack("<H", delay_cs)
        out += b"\x00\x00"
        out += b"\x2C"
        out += struct.pack("<HHHHB", 0, 0, width, height, 0)
        flattened = [value for row in rows for value in row]
        out += lzw_encode(flattened)

    out += b"\x3B"
    return bytes(out)


def validate_frames(frames, width, height):
    """校验帧序列，返回问题列表而不是抛异常。

    参数:
        frames: 二维 RGB 帧的序列。
        width, height: 期望尺寸，必须是正整数。

    返回:
        ``list[str]``，按帧序返回，全部合法时为 ``[]``。消息格式固定为：

        - 空帧（没有行，或某行没有像素）：``"frame {i}: empty frame"``
        - 尺寸不符：``"frame {i}: expected {width}x{height}, got {w}x{h}"``
        - 颜色分量不在 0..255：
          ``"frame {i}: color out of range at ({row}, {col})"``

        同一帧同时有多个问题时按“空帧 -> 尺寸 -> 颜色”的顺序只报第一条；
        颜色问题每帧最多一条。

    异常:
        width/height 不是正整数时抛 ``GifError``。
    """
    _require_positive_int(width, "width")
    _require_positive_int(height, "height")
    if not isinstance(frames, (tuple, list)):
        raise GifError("frames must be a sequence")

    problems = []
    for index, frame in enumerate(frames):
        # 1) 空帧：没有行，或某行没有像素。
        if not isinstance(frame, (tuple, list)) or len(frame) == 0:
            problems.append(f"frame {index}: empty frame")
            continue
        is_empty = False
        for row in frame:
            if not isinstance(row, (tuple, list)) or len(row) == 0:
                is_empty = True
                break
        if is_empty:
            problems.append(f"frame {index}: empty frame")
            continue

        # 2) 尺寸：行数与每行列数都要精确匹配。
        got_h = len(frame)
        got_w = len(frame[0])
        size_ok = got_h == height and all(
            isinstance(row, (tuple, list)) and len(row) == width for row in frame
        )
        if not size_ok:
            problems.append(
                f"frame {index}: expected {width}x{height}, got {got_w}x{got_h}"
            )
            continue

        # 3) 颜色分量 0..255，每帧最多报一条。
        bad = None
        for row_index, row in enumerate(frame):
            for col_index, color in enumerate(row):
                if not _is_valid_raw_color(color):
                    bad = f"frame {index}: color out of range at ({row_index}, {col_index})"
                    break
            if bad is not None:
                break
        if bad is not None:
            problems.append(bad)

    return problems


def encode_batch(animations, config):
    """批量把多个动画编码成 GIF，每个动画用独立调色板，失败整批回滚。

    参数:
        animations: 序列，元素为 dict::

            {"name": str, "frames": 二维 RGB 帧序列, "width": int,
             "height": int, "durations_ms": list[int]}

        config: dict::

            {"max_colors": int (2..256), "dither": float (0..1),
             "loop": int (>= 0)}

    返回:
        dict::

            {"count": 动画数,
             "total_bytes": 所有 GIF 字节数之和,
             "animations": [{"name": str,
                             "palette": list[tuple],
                             "delays_cs": list[int],
                             "gif": bytes}, ...]}

        返回列表的顺序必须与输入完全一致；每个动画单独调用 ``median_cut``
        得到自己的调色板，不能让后一个动画复用/污染前一个动画的调色板
        （否则颜色会串）。

    规则:
        - 逐帧用 ``config["dither"]`` 选择 ``dither`` 或 ``map_to_palette``，
          再 ``assemble``；
        - 先对全部动画做校验（尺寸、空帧、颜色越界、延迟、config），任一动画
          非法立即抛 ``GifError``，不返回部分结果、也不留下影响后续调用的状态；
        - 后续合法批次的结果必须与全新调用完全一致。

    异常:
        任一动画或 config 非法时抛 ``GifError``。
    """
    if not isinstance(config, dict):
        raise GifError("config must be a dict")
    max_colors = config.get("max_colors")
    if not _is_int(max_colors) or not 2 <= max_colors <= MAX_COLORS:
        raise GifError("config['max_colors'] must be an integer in 2..256")
    dither_strength = config.get("dither", 0.0)
    if isinstance(dither_strength, bool) or not isinstance(
        dither_strength, (int, float)
    ):
        raise GifError("config['dither'] must be a number in 0.0..1.0")
    dither_strength = float(dither_strength)
    if not math.isfinite(dither_strength) or not 0.0 <= dither_strength <= 1.0:
        raise GifError("config['dither'] must be a number in 0.0..1.0")
    loop = config.get("loop", 0)
    if not _is_int(loop) or loop < 0 or loop > 0xFFFF:
        raise GifError("config['loop'] must be an integer in 0..65535")

    if not isinstance(animations, (tuple, list)):
        raise GifError("animations must be a sequence")

    # 第一阶段：先对全部动画做校验，任一非法立即抛错，不产生部分结果。
    prepared = []
    for index, animation in enumerate(animations):
        if not isinstance(animation, dict):
            raise GifError(f"animation {index} must be a dict")
        name = animation.get("name")
        if not isinstance(name, str):
            raise GifError(f"animation {index}: name must be a string")
        width = animation.get("width")
        height = animation.get("height")
        _require_positive_int(width, "width")
        _require_positive_int(height, "height")
        frames = animation.get("frames")
        if not isinstance(frames, (tuple, list)) or not frames:
            raise GifError(f"animation {index}: frames must be a non-empty sequence")
        problems = validate_frames(frames, width, height)
        if problems:
            raise GifError("; ".join(problems))
        delays_ms = animation.get("durations_ms")
        if not isinstance(delays_ms, (tuple, list)) or len(delays_ms) != len(frames):
            raise GifError(
                f"animation {index}: durations_ms must match the number of frames"
            )
        delays_cs = quantize_delay(delays_ms)
        prepared.append(
            {
                "name": name,
                "frames": frames,
                "width": width,
                "height": height,
                "delays_cs": delays_cs,
            }
        )

    # 第二阶段：全部合法后才真正编码；每个动画使用自己独立的调色板。
    results = []
    for item in prepared:
        frames = item["frames"]
        palette = median_cut(frames, max_colors)
        planes = []
        for frame, delay_cs in zip(frames, item["delays_cs"]):
            if dither_strength > 0.0:
                plane = dither(frame, palette, dither_strength)
            else:
                plane = map_to_palette(frame, palette)
            planes.append((plane, delay_cs))
        gif = assemble(planes, palette, item["width"], item["height"], loop)
        results.append(
            {
                "name": item["name"],
                "palette": palette,
                "delays_cs": list(item["delays_cs"]),
                "gif": gif,
            }
        )

    return {
        "count": len(results),
        "total_bytes": sum(len(item["gif"]) for item in results),
        "animations": results,
    }
