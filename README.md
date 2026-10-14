# Suno MIDI 整音（MIDI Tidy）

用同一条分轨的 WAV 反过来修正 Suno 导出的 MIDI。

不采信 MIDI 里写的力度，一切回到音频里找证据；只删音频里确实没响的音符；
把 WAV 与 MIDI 之间的起音错位量算出来、报告清楚、再决定是否对齐。

## 完全免费，全功能开放

本技能**完全免费**，无需授权码，全程离线，不联网、不埋点：

| 能力 | 状态 |
|------|:----:|
| 起音检测 + **自动对齐** | ✅ |
| 力度重建（精细，最多 107 档） | ✅ |
| 重复音清理 / 无声音符清理 | ✅ |
| 人声音高核验 | ✅ |
| `score/` 出谱版 + `mix/` DAW 版 + 拍号写入 | ✅ |
| 批量处理 / 长音频 / 逐音删除清单 | ✅ |

* 查看能力开放情况：`python scripts/midi_tidy.py --show-license`
* 能力总览与历史分层说明：`FREE_vs_PRO.md`

以 **MIT 许可证**发布，可自由使用、修改、再分发、商用（详见 [`LICENSE`](LICENSE)）。

## 获取与安装

```bash
git clone https://github.com/USERNAME/suno-midi-tidy.git
cd suno-midi-tidy
pip install -r requirements.txt
python scripts/selftest.py          # 先确认环境和算法都正常
```

依赖只有 `numpy` 与 `pretty_midi` 是必需的；缺 `scipy` / `soundfile` 时会自动降级，
功能不受影响。

想把本技能装进 AI 助手直接用，把整个目录放到对应位置即可（脚本之间全用相对路径
引用，改挂到哪个平台都能跑）：

| 平台 | 安装位置 |
|------|---------|
| WorkBuddy | `~/.workbuddy/skills/suno-midi-tidy/`（用户级）或 `.workbuddy/skills/`（项目级） |
| Codex | `~/.codex/skills/suno-midi-tidy/`（用户级）或 `.codex/skills/`（项目级） |
| Claude Code / Cursor | `.claude/skills/`、`.cursor/skills/` |

## 快速开始

```bash
# 1. 环境自检
python -c "import numpy, pretty_midi, soundfile; print('deps ok')"

# 2. 先跑答案已知的合成测试（不可跳过）
python scripts/selftest.py

# 3. 只分析，看偏移量
python scripts/midi_tidy.py --input "分轨文件夹" --out "整理输出" --dry-run

# 4. 正式整理（拍号一定要给对）
python scripts/midi_tidy.py --input "分轨文件夹" --out "整理输出" --time-sig 3/4 --yes
```

想用图形界面（含文件夹选择）：

```bash
python scripts/start_gui.py             # 启动界面
python scripts/start_gui.py --check     # 只做环境自检
```

解释器没有 tkinter 时会自动降级为文本交互，功能完全一致。
若拿到的是**作者直发包**，Windows 上还可以双击 `RUN_WINDOWS.bat`：它会自建
`.venv`（不污染系统 Python）、装好依赖再启动界面。技能市场版不含 `.bat`
（`.bat` / `.cmd` 属可执行脚本，平台审核不接受）。

## 输入素材

同一条分轨的 `.mid` 与 `.wav` 放同一个文件夹，文件名必须相同：

```
我的歌 (Bass).mid     我的歌 (Bass).wav
我的歌 (Drums).mid    我的歌 (Drums).wav
我的歌 (Vocal).mid    我的歌 (Vocal).wav
```

## 输出

```
整理输出/
├── mix/      演奏时间细节版（Cubase / Ableton）
├── score/    量化版（MuseScore 出谱），拍号已写入
└── report/   每轨统计 CSV + 完整参数 JSON
    └── *_removed_notes.csv  被删音符逐个清单
```

原始文件不会被修改。

## 整理完之后

把 `mix/` 里的 MIDI 和原 WAV 一起拖进 DAW 叠着播放，然后打开
`report/*_removed_notes.csv` **逐个回听被删掉的音**。删错了调阈值重跑，
不要在 DAW 里手工补——手工补的下一次整理还会丢。

## 反馈

遇到删错音、对齐不准、人声清理不干净，先看 `references/pitfalls.md` 的排查顺序，
再调阈值重跑。所有判定过程都写进了 `report/`，可以直接拿去定位问题。

## 详细文档

| 文档 | 内容 |
|------|------|
| `LICENSE` | MIT 许可证（授权条款以此为准） |
| `SKILL.md` | 完整用法、判据、限制 |
| `EDITION.md` | 本包版本说明（全功能免费） |
| `FREE_vs_PRO.md` | 能力总览与历史分层说明 |
| `EULA.md` | 免责声明与第三方依赖说明（授权已让位于 MIT） |
| `references/methodology.md` | 四个问题从哪来、怎么被发现的 |
| `references/thresholds.md` | 每个默认阈值的实测标定与隔离带 |
| `references/pitfalls.md` | 已知坑与排查顺序 |

## 开发与测试

```bash
python scripts/selftest.py                        # 61 项断言 / 7 组，全 PASS 才继续
python scripts/synth_fixture.py _smoke            # 生成答案已知的合成素材
python scripts/midi_tidy.py --input _smoke --out _test_out --dry-run
```

改动引擎后请以**全部通过、0 失败**为准（不要以固定项数判据，用例会增删）。
推送到 `main` 时 GitHub Actions 会自动在 Python 3.12 / 3.13 上复跑这套用例
（见 `.github/workflows/selftest.yml`）。

打包：

```bash
python build_skillhub.py --out dist-hub --flat-zip   # SkillHub 上架包（15 项自检）
python 打包脚本 --out dist                  # 分发包
```

## 仓库内容

```
suno-midi-tidy/
├── scripts/          引擎与入口（tidy_core / midi_tidy / gui / selftest）
├── references/       方法论、阈值标定、坑位排查（含发行方决策文档）
├── 商户端目录           商户端签发工具（仅脚本；私钥不入库）
├── build_skillhub.py 打 SkillHub 上架包
└── 打包脚本 打分发包
```

> `商户私钥文件`（授权码私钥）**不在仓库中**，由 `.gitignore` 拦截。
> 该文件是历史授权体系的唯一签发凭据，仅离线备份，绝不进入任何仓库或网盘。

## 许可证

[MIT](LICENSE) © 2026 罗心璞
