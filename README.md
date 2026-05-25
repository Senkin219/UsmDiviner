# UsmDiviner

UsmDiviner 是一个用于处理 CRI USM 文件的命令行工具。它会尝试从视频流恢复 USM 密钥，提取解密后的视频和音频，并在找到 vgmstream 时自动把音频转为 WAV。

## 功能

- 无需预置密钥，自动尝试恢复 USM 密钥
- 可处理单个 USM 文件，也可递归处理目录中的 `.usm` 文件
- 默认启用多进程，可用 `--no-parallel` 关闭

## 获取 vgmstream (Windows)

vgmstream 用于将 HCA/ADX 解码为 WAV。

1. 打开 <https://vgmstream.org/>
2. 下载包含 `vgmstream-cli.exe` 的 Windows 命令行版本
3. 解压后，将整个文件夹放到项目根目录，并改名为 `vgmstream-win64`

推荐目录位置：

```text
UsmDiviner/
├─ UsmDiviner.py
├─ usmdiviner/
└─ vgmstream-win64/
   └─ vgmstream-cli.exe
   └─ *.dll
   └─ ...
```

保持上述路径时，UsmDiviner 会自动找到 vgmstream。

## 使用方法

需要 Python 3.10 或更高版本。

```bash
# 获取项目
git clone https://github.com/Senkin219/UsmDiviner.git
cd UsmDiviner

# 处理单个文件
python UsmDiviner.py input.usm

# 递归处理目录中的 .usm 文件
python UsmDiviner.py ./USM

# 指定输出目录
python UsmDiviner.py input.usm -o output

# 使用 ffmpeg 封装为 MKV
python UsmDiviner.py input.usm --mux-mkv --ffmpeg "D:/tools/ffmpeg/bin/ffmpeg.exe"
```

## 命令行选项

| 选项 | 说明 |
|---|---|
| `input` | USM 文件或包含 `.usm` 的目录 |
| `-o, --output` | 输出目录，默认为 `output` |
| `--no-parallel` | 关闭多进程 |
| `--report` | 为每个 USM 生成 `report.json` |
| `--fast` | 仅使用前 50 MB 视频数据恢复密钥 |
| `--key KEY` | 手动指定 16 位十六进制 USM 密钥 |
| `--extract-only` | 不解密视频和音频流，仅原样提取 |
| `--vgmstream PATH` | 手动指定 vgmstream-cli 路径 |
| `--keep-intermediate-audio` | 音频解码成功后保留 `.hca/.adx` 和 `.hcakey` 文件 |
| `--no-adx-audiomask` | 不对 ADX 应用 AudioMask，默认结果为杂音时可尝试使用 |
| `--mux-mkv` | 使用 ffmpeg 封装为 MKV |
| `--audio-language-preset <gamename>` | MKV 音轨语言 metadata 预设，默认为 `none`；会根据指定游戏的音轨约定映射 language metadata，当前支持 `gi`；需要配合 `--mux-mkv` 使用 |
| `--ffmpeg PATH` | 手动指定 ffmpeg 路径 |

## 输出

默认输出到：

```text
output/<usm文件名>/
```

常见文件：

```text
<name>.ivf      解密后的视频流
<name>_ch0.wav  vgmstream 解码后的音频
<name>.mkv      开启 --mux-mkv 且封装成功时生成
report.json     仅开启 --report 时生成
```

中间文件 `.hca/.adx/.hcakey` 默认会在音频解码成功后删除。MKV 封装成功后，已提取的视频和音频流也会被删除。

## Test

UsmDiviner has been tested against all Genshin Impact USM assets available through Version Luna VI. All assets were processed correctly except for one file whose video stream is too small for reliable key recovery.

## Credits

- USM chunk parsing and mask/key handling were implemented with reference to [GI-cutscenes](https://github.com/ToaHartor/GI-cutscenes).
- The blind key recovery algorithm was provided by Gemini.
- Audio decoding is delegated to [vgmstream](https://vgmstream.org/). This repository does not include vgmstream binaries.
