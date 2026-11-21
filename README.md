# video-gif-encode

## 题面

把录屏导出 GIF 后，颜色断层严重、播放速度还快了一倍：中位切分按通道数整除，调色板不足 2 色；最近颜色用欧氏距离但未开方比较，选错色；抖动误差不扩散，画面出现色带；延迟直接取整，1 ms 变成 0 导致播放器加速；LZW 码宽到达 12 位不重置字典，编码越界；GIF 头缺 NETSCAPE 循环块，只播一次；帧尺寸不符不报错，越界读；颜色值 256 不钳制，索引溢出；批量共用调色板，动画之间串；max_colors<2 不校验，除零。gifenc/palette.py 是核心，tests/test_palette.py 15个红。要修的是：median_cut 的中位切分（按像素数而不是通道数切、校验 max_colors）、map_to_palette 的最近色与 0..255 钳制、dither 的 Floyd-Steinberg 误差扩散、quantize_delay 的厘秒下限、lzw_encode 的码宽增长与 12 位字典重置、assemble 的 NETSCAPE 循环块与 GIF 块结构、validate_frames 的尺寸/空帧/颜色校验、encode_batch 的独立调色板与整批回滚。

## 运行方式

```powershell
cd D:\gsb-20261119\video-gif-encode
python -m pytest tests -q
```

初始（未修复）状态必须恰好是：

```
15 failed
```

15 条测试全部因 `NotImplementedError` 失败，没有 collection error / import error / syntax error。
按各函数 docstring 的契约实现后，同一套测试应为 `15 passed`。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 音视频处理 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## tests 不可修改

`tests/test_palette.py` 不可修改：不得删除、跳过、重命名或弱化任何断言；只能修改
`gifenc/` 下的实现，让 15 条测试全部变绿。实现只允许使用标准库 `math`、`struct`
与 `pytest`，离线、无随机。
