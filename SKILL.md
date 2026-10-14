---
name: suno-midi-tidy
display_name: "Suno MIDI 整音"
display_name_en: "Suno MIDI Tidy"
description: 用同一条分轨的 WAV 反过来修正 Suno 导出的 MIDI——重建演奏力度、清掉重复音、只删 WAV 里确实没响的音符、检测并报告 WAV 与 MIDI 的起音错位，最后产出 score（量化出谱）与 mix（保留演奏细节）两套 MIDI 加 CSV 报告。当用户提到「Suno 导出的 MIDI 太难用」「MIDI 力度全是同一个值 / 没有强弱」「MIDI 导出后拍号变成 4/4」「WAV 和 MIDI 对不齐 / 有错位 / 慢了半拍」「MIDI 里一堆细碎重复音」「人声 MIDI 冒出高八度和弦 / 混进低音 / 串进伴奏」「把 MIDI 整理成能出谱的文件」「MIDI 清理 / MIDI 整音 / midi tidy / Suno MIDI Lab」时使用。也适用于任何「有配套音频的转写 MIDI（Basic Pitch、Suno、Melodyne 等）需要修整后进 DAW 或 MuseScore」的场景。
description_zh: "Suno MIDI 整音：从 WAV 重建力度、清重复音与哑音、检测起音错位，输出 score/mix 两套 MIDI 与报告"
description_en: "Repair Suno-exported MIDI using its paired WAV: rebuild velocity, drop duplicates and silent notes, detect onset offset, emit score/mix MIDI plus a report"
version: 1.1.0
category: "Music"
platforms: ["WorkBuddy", "QClaw", "ima", "Claude Code", "Cursor", "Codex"]
tags: ["MIDI", "Suno", "音频处理", "力度重建", "起音对齐", "编曲", "DAW", "MuseScore", "pretty_midi", "AI音乐"]
author: "罗心璞"
visibility: "public"
agent_created: true
allowed-tools: Bash,Read,Write,Edit,Glob,Grep
metadata:
  tags: ["MIDI", "Suno", "音频处理", "DAW", "MuseScore", "pretty_midi", "编曲"]
  clawdbot:
    emoji: "🎹"
    requires:
      bins: ["python"]
---

# Suno MIDI 整音（MIDI Tidy）

把「音频转出来的 MIDI」修成**能用**的 MIDI。核心思路只有一句：
**不要相信 MIDI 里写的数字，一切回到 WAV 里找证据。**

## 规范与安装位置

本技能采用 Agent Skills 通用结构（`SKILL.md` + YAML frontmatter `name` / `description`
+ `scripts/` + `references/`），**WorkBuddy 与 Codex 共用同一份包**，无需改动：

| 平台 | 安装位置 | 说明 |
|------|---------|------|
| WorkBuddy | `~/.workbuddy/skills/suno-midi-tidy/`（用户级）或 `.workbuddy/skills/`（项目级） | 从技能市场安装，或把发布包解压到该目录 |
| Codex | `~/.codex/skills/suno-midi-tidy/`（用户级）或 `.codex/skills/`（项目级） | 解压发布包改名即可，正文无需修改 |

放在技能目录里的脚本用**相对路径**互相引用（`sys.path.insert(0, dirname(__file__))`），
不依赖任何绝对路径，因此改挂到哪个平台都能直接跑。

> 安装（或从外部导入）任何技能前，先按本机的 skills 安全审计流程过一遍脚本与
> 依赖清单，再决定是否启用。

## 〇、完全免费，全功能开放

本技能**完全免费**：所有能力默认开放，**无需授权码**，全程离线，不联网、不埋点。

| 能力 | 状态 |
|------|:----:|
| 起音偏移检测 + **自动对齐** | ✅ |
| 力度重建（精细，最多 107 档） | ✅ |
| 重复音清理 / **无声音符清理** | ✅ |
| **人声音高核验**（去串音、假八度） | ✅ |
| 输出 `score/`（量化出谱）与 `mix/`（DAW 继续编曲） | ✅ |
| 拍号写入（3/4 等） | ✅ |
| 批量处理（多个配对）/ 长音频 | ✅ |
| 逐音删除清单（可回听审计） | ✅ |

* **当前能力开放情况**：`python scripts/midi_tidy.py --show-license`
* **能力总览**：`FREE_vs_PRO.md`；**本包说明**：`EDITION.md`。
* 完整用法见下文；判据与阈值的实测标定见 `references/thresholds.md`。

> **历史沿革**：本技能曾按「免费版 / 专业版」两档分发，专业版凭 `license.key`
> （Ed25519 离线签名）解锁。现两档已**合并为单一全功能免费版**——原先锁定的
> mix 输出、拍号写入、无声清理、人声核验、精细力度、批量与长音频，全部默认可用。
> 老用户的 `license.key` 仍然有效，但已不再影响任何功能的放行。

## 一、要解决的四个真实问题

均来自日本创作者熊野ぴ太郎的公开发开日志（「MIDI整音」/ MIDI Tidy）：

| 问题 | 现象 | 本技能的做法 |
|------|------|--------------|
| **力度是假的** | 1056 个贝斯音符里 86% 的 Velocity 都是 53，全轨只有 7 种数值；与对应 WAV 音量的相关系数只有 **-0.001** | 丢弃原始 Velocity，在每个音符起点后取 90ms 窗口的真实音量重建 |
| **清理越卖力，音乐越少** | 按「又短又弱」删杂音，被删掉的音符里 **64%** 在原始音频里仍有声音；最夸张的一首删掉了 59% 的音符 | 只删「WAV 里确实没声」的音符，且用**局部**窗口判断，全程留删除记录 |
| **安静乐段被整段误删** | 前奏约 -42dB、主歌 -24dB，拿整首歌最响处当基准，前奏 40 个真实音符被当成杂音 | 基准取「前后约 4 秒内邻近音符」的响度高分位，不用全曲值 |
| **WAV 和 MIDI 差 330ms** | 七首歌实测 WAV 起音比 MIDI 晚 **40–334ms**，且每首不同。120BPM 下一拍才 500ms | 能量突变位置 × Note On 时间互相关，逐轨估计取中值，并识别「整拍歧义」 |

人声分轨还有两个附加问题：冒高八度和弦、混进整串低音（伴奏串进主唱轨）。
音量检测对它无效——歌手正在唱时，假音符所在位置同样有声音。因此人声走**音高证据**判据。

## 二、铁律

1. **原始文件永不修改。** 只往输出目录写。输出目录不能位于输入目录内部（代码会拒绝）。
2. **对齐必须排在力度与清理之前。** 否则原本属于贝斯第一下的声音会被记到第二个音符头上。
3. **先造一组答案已知的测试音乐，再碰真实歌曲。** 程序生成的 MIDI 通常都能打开，实际听起来却可能是错的。不跑通 `selftest.py` 就不要处理真实分轨。
4. **偏移量先报告、后应用。** 默认流程是先分析让你确认；批量处理时才加 `--yes`。
5. **删除必须留档。** 每次运行都产出 `*_removed_notes.csv`，要能逐个回听。
6. **宁可不删。** 删除需要同时满足两个条件（局部信噪比低 **且** 明显弱于邻近音符）。不确定就用 `--conservative`。

## 三、准备素材

同一条分轨的 `.mid` 与 `.wav` 放同一个文件夹，**文件名必须相同**：

```
我的歌 (Bass).mid     我的歌 (Bass).wav
我的歌 (Drums).mid    我的歌 (Drums).wav
我的歌 (Vocal).mid    我的歌 (Vocal).wav
```

- 已经在 Suno Studio 导出过 MIDI 的直接用。
- 只有 WAV 的话，可以先用 Spotify 开源的 Basic Pitch 转出 MIDI（单声部贝斯、独奏旋律效果好；复调乐器需要更多人工校对），再交给本技能整理。
- 录音质量差、底噪很高的素材，把 `--snr-db-min` 调高（见阈值表）。

## 四、标准流程

### 第 0 步：环境自检

```bash
python -c "import numpy, pretty_midi, soundfile; print('deps ok')"
```

缺依赖时装（国内走清华镜像）：

```bash
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> 若本机已有装好依赖的 Python 环境（numpy / scipy / soundfile / pretty_midi 齐备），
> 直接用它的绝对路径替换下面的 `python` 即可，例如
> `/path/to/venv/bin/python scripts/midi_tidy.py ...`（Windows 用 `D:\py\Scripts\python.exe` 同理）。

### 第 1 步：先跑合成测试（**不可跳过**）

```bash
python scripts/selftest.py
```

它会生成三组答案已知的素材并逐项断言，覆盖：180ms 延迟识别、15 音→13 音（去 1 重复 + 1 哑音）、力度 1 种→≥8 种、安静前奏零误删、人声假音剔除、原始文件未被改动，以及 **Ed25519 的 RFC 8032 官方向量比对**、授权码篡改/过期/换机/换产品的拒绝，还有**全功能开放的回归护栏**（能力集 == 全集、无授权码时默认即产出 mix + score + 逐音清单 + 拍号）。**全部 PASS 才继续。**

把 `--keep` 加上可以保留测试目录，人工听检一遍。

### 第 2 步：只分析，先看偏移量

```bash
python scripts/midi_tidy.py --input "我的歌分轨" --out "整理输出" --dry-run
```

输出形如：

```
=== 我的歌 (Bass) ===
起音偏移: WAV 比 MIDI 晚 180 ms  (相关性 1236.2)
          估计拍周期 499 ms -> 180 ms 约为 0.36 拍
已应用对齐: +180 ms (MIDI 时间轴)
轨道                           原始       保留       重删       哑删       力度种类
Bass                         15       13        1        1      1→13
```

看到这一行再决定是否继续。若报告里的偏移量接近一个整拍，说明可能是鼓组循环造成的整拍歧义——换一条乐器轨的结果对照，或用 `--no-align` 只处理力度。

### 第 3 步：正式整理

```bash
python scripts/midi_tidy.py --input "我的歌分轨" --out "整理输出" --time-sig 3/4 --yes
```

拍号一定要显式给对——Suno 导出的 MIDI 拍号常常一律写成 4/4，一首三拍子华尔兹也会是 4/4，进 MuseScore 后小节全乱。`--time-sig` 会把正确的拍号写进 MIDI 元事件。

想用图形界面（含文件夹选择）：

```bash
python scripts/start_gui.py            # 启动界面
python scripts/start_gui.py --check    # 只做环境自检，不启动
```

解释器没有 tkinter 时会自动降级为文本交互，功能完全一致。

> 若拿到的是**作者直发包**，Windows 上还可以双击 `RUN_WINDOWS.bat`：它会自建
> `.venv`、装好依赖再启动界面。技能市场版**不含** `.bat`——`.bat` / `.cmd`
> 属于可执行脚本，平台审核一律不接受（见 `references/pitfalls.md`）。

### 第 4 步：回听验证（这一步不能省）

1. 把 `mix/` 里的整理后 MIDI 和原 WAV 一起拖进 DAW，叠着播放。
2. 打开 `report/*_removed_notes.csv`，**逐个回听被删掉的音**。若删错，调高 `--snr-db-min` 或直接用 `--no-delete-silent` 重跑。
3. 人声串音没干净，再调 `--vocal-min-evidence-db`；谱面仍然乱，才考虑加节拍、拍号、MusicXML 输出。

## 五、输出结构

```
整理输出/
├── mix/      演奏时间细节版（Cubase / Ableton 继续编曲）
├── score/    按栅格量化版（MuseScore 出谱），拍号已写入
└── report/
    ├── <曲名>_report.csv         每轨：原始/保留音符数、各类删除数、力度种类前后对比、偏移量
    ├── <曲名>_removed_notes.csv 被删音符逐个清单（音名、时间、删除原因、局部 SNR）← 回听就看这个
    └── <曲名>_summary.json      完整参数与警告，便于复现
```

`mix` 与 `score` 的差别只在时间处理：`mix` 原样保留演奏时间（可含亚毫秒抖动），`score` 量化到 `--grid` 指定的栅格并保证音符长度不为零。力度、清理结果两者完全一致。

## 六、判据与阈值

| 参数 | 默认 | 作用 | 调法 |
|------|------|------|------|
| `--max-offset-ms` | 450 | 起音偏移搜索上限 | 实测最大 334ms，一般不用改 |
| `--context-sec` | 4.0 | 局部响度上下文半径 | 段落切换很频繁时调小到 2–3 秒 |
| `--snr-db-min` | 4.0 | 判「没声音」的局部信噪比 | 底噪大就调到 6–8；误删多就先关掉删除 |
| `--note-win-ms` | 90 | 力度取样窗 | 打击乐调到 30–50ms 更能抓到撞击瞬间 |
| `--dup-tol-ms` | 35 | 重复音起点容差 | 转写抖动大调到 50 |
| `--grid` | 4 | score 版量化栅格（1/N 分音符） | 爵士/自由速度用 8 甚至不量化 |
| `--vocal-min-evidence-db` | 10 | 人声保留所需谐波余量 | 真实音被删就降到 6；串音没清干净升到 14 |
| `--conservative` | — | 保守预设 | SNR 提到 8dB、重复音容差收到 25ms、关掉人声清理 |

阈值怎么定的、为什么用「同频段非谐波内容的 95 百分位」当人声参考（而不是全频段中位谱）、以及各判据的实测隔离带，见 `references/thresholds.md`。

## 七、已知限制（必须如实告知用户）

- **复调乐器（钢琴和弦）力度不可精确还原。** 几个音同时响在同一条 WAV 里，程序无法判断是哪根手指更用力。流程会自动标记「复调不确定音」并在报告里给出警告，需人工复核。
- **和声、合唱、颤音、滑音超出当前人声判据的适用范围**（单人主唱效果好）。最终判断仍要放回歌曲里听。
- **八度上方的误判只报告不自动改音高**（报告里出现 `suspect-octave-up`）。自动移调的风险大于收益。
- 起音对齐用的是**能量突变**而非严肃的 onset detection 库；对极弱的弦乐起音、连续渐强的 pad，相关性会下降，此时以报告里的相关性数值为准判断可信度。

## 八、扩展新功能

这个技能本身就是「一份写得足够细的需求说明」，可以继续长。改之前先记住循环：

1. 在 `scripts/synth_fixture.py` 里**加一组会失败的合成素材**（把想要的新行为对应的错误故意插进去）。
2. 在 `scripts/selftest.py` 里加断言，先看它 FAIL。
3. 改 `scripts/tidy_core.py`，直到全绿。
4. 最后才拿真实分轨验证。

已有的自然下一步：鼓组按 GM 映射整理、节拍/拍号检测与 MusicXML 导出、整体移调与调号、`score` 版的分谱（一行一乐器）输出。

## 文件清单

| 文件 | 作用 |
|------|------|
| `scripts/tidy_core.py` | 核心引擎：配对、偏移检测、力度重建、清理、人声核验、输出 |
| `scripts/midi_tidy.py` | 命令行入口（含 `--show-license` / `--machine-id`） |
| `scripts/gui.py` | 图形界面（无 tkinter 时自动降级为文本交互） |
| `scripts/start_gui.py` | 图形界面启动器（跨平台；`--check` 只做环境自检） |
| `scripts/licensing.py` | 能力矩阵（引擎唯一功能清单）+ 离线授权校验（历史继承，当前不限制任何功能） |
| `scripts/_ed25519.py` | 纯 Python Ed25519（RFC 8032 向量已验证） |
| `scripts/_pubkey.py` | 内嵌验签公钥，仅供历史授权码离线校验 |
| `scripts/synth_fixture.py` | 生成三组答案已知的测试素材 |
| `scripts/selftest.py` | 自动化断言（引擎 + 加密 + 全功能回归） |
| `edition.json` | 本包声明的默认版本 |
| `EDITION.md` | 本包版本说明（全功能免费） |
| `FREE_vs_PRO.md` | 能力总览与历史分层说明 |
| `LICENSE` | MIT 许可证（授权条款以此为准） |
| `EULA.md` | 免责声明与第三方依赖说明（授权已让位于 MIT） |
| `references/methodology.md` | 方法论：原文开发日志的完整推导与踩坑顺序 |
| `references/thresholds.md` | 阈值标定过程与实测隔离带 |
| `references/pitfalls.md` | 已知坑与错误恢复 |
