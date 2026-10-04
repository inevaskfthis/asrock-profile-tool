# asrock-profile-tool

编辑 ASRock BIOS 的 **U 盘用户配置档案**，用来改那些 **BIOS 菜单里没有、但固件真正会读取** 的 UEFI Setup 变量字节 —— 例如 **Load-Line Calibration（防掉压 / LLC）**。

Edit ASRock BIOS **USB user-profile** files to modify UEFI Setup variable bytes that the firmware **actually reads** but the BIOS menu no longer exposes — for example **Load-Line Calibration (LLC)**.

**不需要刷 BIOS、不需要编程器、不需要动签名区、不需要改 IFR，完全可逆。**
**No BIOS flashing, no SPI programmer, no signed-region writes, no IFR editing — and fully reversible.**

**中文** ｜ [English](#english-documentation)

> 想知道**背后的原理**（为什么菜单里没有这个条目却仍然有效、为什么读回来判断当前值这么难），
> 看 [HOW-IT-WORKS.md](HOW-IT-WORKS.md)。

---

# 中文

## 这是什么

ASRock 的 BIOS 里有 **`Save User Default to USB flash drive`** / **`Load User Default from USB flash drive`**。
导出的档案里内嵌着**一整份 UEFI Setup 变量的原样副本**；载入档案时，由固件自己把它写回活体变量。

本工具就改这份副本里的指定字节 —— 从而**绕过所有写入限制**，直接改到那个"菜单里看不见、但后端会读"的值。

> **实测结论（ASRock X370 Killer SLI 10.50）**：把 `Setup+0x190` 改成 `03` 并载入档案后，
> CPU LLC Level 3 **重启后依然有效**；读回 SPI 能确认 NVRAM 里出现了带该值的新副本。

## 为什么需要它

很多 ASRock 主板（尤其是被"降级"过的板型）在 BIOS 里删掉了部分超频选项：

- 选项的**界面条目**没了，但
- 底层的 **UEFI 变量槽位和驱动消费逻辑其实都还在**

以 **X370 Killer SLI 10.50** 为例：

| 项 | 情况 |
|---|---|
| `Setup+0x190` CPU Load-Line Calibration | **IFR 里 0 次引用**（菜单里根本看不到），但**值确实会被消费** ✅ |
| `Setup+0x191` VDDCR_SOC Load-Line Calibration | 菜单里有，正常 |

直接写 NVRAM 不行 —— 运行期 `SetVariable` 被 SMM 全局写锁拦死，写整块 Setup 又要过 Secure Flash。
**BIOS 自带的"用户配置档案"功能就是那条干净的通道。**

```
BIOS: Save User Default to USB   →  档案里嵌着一整份 Setup 变量副本
       ↓ 本工具改这份副本里的一两个字节
BIOS: Load User Default from USB →  固件自己把它写回活体变量，并持久化
```

## 依赖

- Python **3.8+**
- **零第三方依赖**（只用标准库；GUI 用 `tkinter`，Python 官方版自带）
- Windows / Linux / macOS 均可

## 快速开始

### 方式一：图形界面（推荐给不熟命令行的）

```
asrock_profile            # 不带任何参数 = 打开 GUI
asrock_profile --gui      # 等价写法
asrock_profile gui saved_profile   # 启动时直接打开某个档案
```

界面分六块，从上到下走一遍就行：

1. **档案** —— 打开 BIOS 导出的那个档案
2. **档案信息与检查** —— 板型、版本、Setup 块位置；**板型不对会红字警告**
3. **已知字段当前值** —— 只读表格，用来核对偏移表是否匹配（见下节）
4. **修改** —— CPU LLC / SOC LLC 下拉框（`Auto` / `Level 1..5`），外加任意字节 `偏移=值`
5. **输出** —— 输出路径、是否覆盖原文件、**仅预览**
6. **日志** —— 完整操作记录 + 字节级 diff + 自检结果

改完后点 **应用并写出**，再去 BIOS 载入那个档案。

### 方式二：命令行

```bash
# 1) 看档案信息和当前值
asrock_profile info pbo2-test

# 2) 设定 CPU Load-Line = Level 3（0=Auto，1..5=Level）
asrock_profile set pbo2-test --llc 3

# 3) 同时设定 SOC LLC 和另一个字节
asrock_profile set pbo2-test --soc-llc 5 --byte 0x1B1=1

# 4) 只看改动，不写文件
asrock_profile set pbo2-test --llc 3 --dry-run

# 5) 读单个字段
asrock_profile get pbo2-test cpu-llc

# 6) 导出 / 回写整块 Setup 数据（高级玩法）
asrock_profile dump   pbo2-test setup.bin
asrock_profile inject pbo2-test setup.bin -o newprofile

# 7) 检测当前 CPU LLC 是几级（读整片 SPI 镜像，见下节）
asrock_profile detect r5.bin --baseline r4.bin
```

> **分发单文件 exe 时**，无参数运行 = 打开 GUI，带子命令 = 命令行。两者同一个文件。

## 语言 / Language

界面与输出支持**中文 / English**：

| 方式 | 写法 |
|---|---|
| 命令行选项 | `asrock_profile --lang en info pbo2-test`（写在子命令前后都行） |
| 环境变量 | `ASR_LANG=en`（或 `zh`） |
| GUI | 顶部「界面语言」下拉框 —— **界面立即重建**，已载入的档案、待改动与日志都会保留 |
| 默认 | 自动判断：`$ASR_LANG` → 终端能不能显示中文 → 系统 locale |

**自动判断的实际效果**：中文 Windows（cp936 控制台）→ 中文；
终端编码显示不了中文时（英文/欧洲语系控制台、`PYTHONIOENCODING=ascii`）→ **自动切成英文**，
不会再甩一屏 `?` 给你。POSIX 上参考 `LC_ALL` / `LANG`；Windows 上**故意不看**它们
（Git Bash 会塞一个 `en_US.UTF-8`，那只是 Git 的默认值，不代表用户的选择）。

> 实现：中文是「源语言」，`T()` 在英文模式下查文件末尾的 `EN` 译文表（key 就是那句中文原文）。
> **漏译的后果只是显示成中文，不会崩。** `selftest.py` 第 15 节会校验
> 「没有漏译」「没有失效 key」「`%s`/`%d` 占位符逐一对齐」「GUI 切语言后无残留中文」。

## 检测当前 CPU LLC（读整片 SPI 镜像）

CPU LLC 在 BIOS 菜单里**没有条目** ⇒ 你在界面上看不到当前是几级。把 SPI 读出来，让工具分析：

```bash
# 先读出镜像（Windows，管理员权限）
#   先 cd 到 AFUWINx64.EXE 所在目录（AMI AFU 工具包自带），输出路径自己定
AFUWINX64.EXE E:\spi\r5.bin /O

# 分析
asrock_profile detect r5.bin                      # 单张镜像（推断）
asrock_profile detect r5.bin --baseline r4.bin    # 给基线 ⇒ 100% 确定
asrock_profile detect r5.bin --expect cpullc-L3   # 顺便核对「我载入的档案真的生效了吗」
```

输出：

```
=== Setup 副本定位（内容特征串全片扫描） ===
  特征串命中 177 处；与共识副本比对后保留 177 处（差异 0–14 字节）、淘汰 0 处

=== 副本链表（按 NVAR 头的 u24 指针串成；链尾 = 最新写入） ===
  链长     链尾(最新)       链头(最旧)       变体数      尾是否已在别处出现 区域
  113    0x07624F     0x062370     3        是（⇒ 冻结链）            高区 [10]
  55     0x04EA56     0x04227E     12       否（⇒ 新状态）            低区 [11]  ← 活跃链

=== 结论（读活跃链的链尾记录 @0x04EA56） ===
  Setup+0x190  CPU Load-Line Calibration              = 3 (Level 3)
  Setup+0x191  VDDCR_SOC Load-Line Calibration        = 3 (Level 3)
  Setup+0x1A6  VTT_DDR (mV)                           = 650
  Setup+0x1A8  DRAM Voltage (mV)                      = 1300

⇒ 当前 CPU Load-Line Calibration = 3 (Level 3)
  交叉核对: DRAM 1300 mV / VTT_DDR 650 mV ⇒ 符合 DDR4 的 2:1 关系 ✔
```

### 为什么不能简单取「多数值」

镜像里的 Setup 变量有 **150+ 个同源副本**（NVRAM compaction 留下的历史版本），而且**它们的值并不一样** ——
实测那张镜像里 `+0x190` 有 **168 个副本是 `00`、9 个是 `03`**，而真正生效的是 **`03`**。
所以「取多数」和「取偏移最大」**都会给出错误答案**（后者会选中一个陈旧的 flashrom 写入副本）。

### 判据：副本链表

每个副本的 NVAR 记录头里有一个 **u24「下一条」指针**（`NVAR+6`，从数据起点算距离），
把所有副本串成**单向链**；链尾的标志是头 3 字节 = `ff ff ff`。
链内记录严格按**写入顺序**排列 ⇒ **链尾 = 最近一次写入的状态**。

活跃链的判定（单张镜像）：

1. 链长 ≥ 2 才算真 bank（长度为 1 的是散落在外部默认区的孤立副本）
2. 优先「**链尾内容在别的链里找不到**」的那条 —— 活跃链的尾是全新状态；冻结链的尾当初被抄进过活跃链
3. 再比「**链内 payload 变体数**」—— 活跃链记录每一次状态变化（实测 12 种 vs 3 种）
4. **给了 `--baseline` 就直接用「哪条链增长了」判定 —— 100% 确定**

实测交叉验证：同一块板子，改动前读的镜像判为 `Auto`、载入 L3 后读的判为 `Level 3`，与真实情况一致。

> ⚠ **单张镜像属推断**（证据充分，但不是数学证明）。想 100% 确定，加 `--baseline` 给上一次读的镜像。
> 工具会把判定依据一并打印出来，你可以自己复核。

> 💡 也可以直接传一份 U 盘配置档案 —— 那会显示**档案里写明的值**，并明确提示这不是主板上当前生效的值。

## ⚠️ 必须知道的限制

| | |
|---|---|
| ❌ **BIOS 菜单里不会因此多出条目** | 菜单里有没有某个选项由 Setup 模块的 **IFR** 决定，跟档案无关。本工具只改**值**，不能加**条目** |
| ⚠️ **文件长度必须完全不变** | 本工具只做等长原地改写；不要用别的工具改长度 |
| ⚠️ **板型 / BIOS 版本字段会被 BIOS 校验** | 工具不会动它们（`0x00` 板型、`0x20` 版本），请保持原样 |
| ⚠️ **没有校验和 ≠ 怎么改都行** | 实测这个格式确实没有 CRC，但**改超出 Setup 变量范围的字节**可能破坏档案结构 |
| ⚠️ **载入档案 = 用档案内容替换你当前的全部设置** | 正确姿势：**先在 BIOS 里把设置调好 → 导出档案 → 再改字节** |

## 板型 / 版本检查（warning 机制）

本工具的字段偏移表是在 **X370 Killer SLI / Gaming K4 10.50** 上一条条逆出来的。
换个板型或换个 BIOS 版本，Setup 变量的布局**完全可能变** —— 那时按固定偏移改写就是纯盲改。
所以工具会**主动叫一声**：

| 情况 | 行为 |
|---|---|
| 板型不在已知表里 | CLI `info` / `set` 打出 **`[版型警告]`**；GUI 红字提示，**写出前弹窗二次确认** |
| 板型对、版本未收录 | 同上（同板型换版本通常兼容，但布局有变的可能） |
| 板型为空 / 头 32 字节不可打印 | 报 **`[错误]`**，视为不是 ASRock 档案 |

**另有一道与板型无关的数值自检**：DDR4 下 `VTT_DDR` 应约为 `DRAM Voltage` 的一半。
工具会算 `Setup+0x1A6` 与 `Setup+0x1A8` 的 2:1 关系，**对不上就直接提示"不要继续改写"** ——
这条能兜住「板型字段写对了、但偏移表其实不匹配」的情况。

想手工核对，看这几行就够了：

```
Setup+0x1A8  DRAM Voltage (mV)   = 1300     ← 应是合理内存电压（1200–1500 常见）
Setup+0x1A6  VTT_DDR (mV)        = 650      ← 应约为上一行的一半
```

## 支持的字段

基于 **X370 Killer SLI / X370 Gaming K4 10.50** 逆向得到（`Setup` 变量内偏移）：

| 偏移 | 名称 |
|---|---|
| `0x18B` | CPU Vcore Voltage mode |
| `0x18C` | CPU Vcore Fixed Voltage (mV) |
| **`0x190`** | **CPU Load-Line Calibration** ← Killer 上菜单里看不到，但值有效 |
| **`0x191`** | **VDDCR_SOC Load-Line Calibration** |
| `0x194` | CPU Vcore Offset Voltage |
| `0x196` | VDDCR_SOC Voltage mode |
| `0x197` | VDDCR_SOC Fixed Voltage (mV) |
| `0x19B` | VDDCR_SOC Offset Voltage |
| `0x1A4` | VDDP (mV) |
| `0x1A6` | VTT_DDR (mV) |
| `0x1A8` | DRAM Voltage (mV) |
| `0x1AA` | 1.05V_PROM Voltage (mV) |
| `0x1AC` | CPU VDD 1.8 Voltage (mV) |
| `0x1AE` | 2.50V_PROM Voltage (mV) |
| `0x1B0` | VPPM |
| `0x1B1` | VDDCR SOC Voltage |
| `0x1B2` | Vcore Offset Voltage |
| `0x233` | CPU Frequency and Voltage(VID) Change |
| `0x262` | SoC/Uncore OC Mode |

LLC 取值：`0` = Auto，`1..5` = Level 1..5。
任意未知字节也可以用 `--byte 0xNNN=VAL`（GUI 里用"自定义字节"）直接改。

字段别名：`cpu-llc` / `soc-llc` / `vddcr-soc-voltage` / `vcore-offset` / `vddp` / `vtt-ddr` /
`dram-voltage` / `vpmm` / `soc-uncore-oc-mode`；也可以直接写十六进制偏移（`0x190`）。

## 档案格式（供适配其它板型／版本）

实测于 ASRock X370 Killer SLI 10.50：

| 文件偏移 | 内容 |
|---|---|
| `0x00` | 板型字段，ASCII，**32 字节定长**（`"A1818"`） |
| `0x20` | BIOS 版本字段，ASCII，**32 字节定长**（`" 10.50"`） |
| `0x38` | `02 00` |
| `0x3A` | `u32` 档案体长度（`0xD62` = 3426） |
| `0x55` | `u32` **Setup 变量长度前缀**（`0x280`） |
| `0x59`–`0x2D8` | **完整的 Setup 变量 640 字节** |
| `0x2D9`–`0xDB3` | 档案体其余部分（其它变量数据） |
| 之后 | 全零填充到固定文件长度（`61,525` 字节） |

**没有 CRC / 校验和**（crc32、sum32 × 多种范围都试过，头部无校验字段）。

**➡ 因此：`文件偏移 = Setup 变量起始偏移 + VarOffset`**（本例：`0x59 + 0x190 = 0x1E9`）

工具**自动探测** Setup 变量位置，不写死 `0x59`。探测用两条结构性约束：

1. **长度前缀之前必须有 3 字节零填充** —— 排除掉全部"落在 Setup 数据内部"的假候选；
2. **取偏移最小的候选** —— Setup 是档案体里第一个 length-prefixed blob。

> 为什么不能只靠"u32 值落在合理区间"：Setup 数据**自己**就含 `00 01 00 00`（= 0x100）这类序列，
> 实测会造出十几个假候选；而且 Setup 数据本身"小字节比例"很高（89–99%），
> **按相似度排序反而会把假的排到前面**。必须靠上面两条结构性约束。

探测失败时用 `--setup-offset` / `--setup-size` 手工指定。

## 安全性 / 回滚

- 本工具**只改 U 盘上的一个文件**，不接触 BIOS ROM、不接触闪存芯片。
- 默认**不覆盖输入文件**（输出为 `<输入>.mod`）；要覆盖请显式 `--inplace` 或勾选 GUI 的选项。
- 每次写出后都会**自动自检**：重新解析输出，确认
  - 文件长度与源文件完全一致
  - **只有**你指定的那些字节发生了变化
  - 每个目标字节确实等于目标值
- **功能回滚**：在 BIOS 里 `Load User Default from USB` 载入改之前导出的那份原始档案即可。

## FAQ

**Q: 载入后 BIOS 里还是看不到那个选项？**
A: 正常。条目由 IFR 决定，本工具不碰 IFR。档位靠"载入哪个档案"来选择 ——
比如预先做好 `llc-L1` … `llc-L5` 五个文件。

**Q: 支持我的主板吗？**
A: 只要你的 BIOS 有 `Save/Load User Default to/from USB flash drive`，且档案能被 `info` 正确解析，就能用。
板型不在已知表里也没关系（工具会警告，请先核对字段值是否合理）—— 把 `info` 输出贴到 issue，我来补字段表。

**Q: 会不会把主板搞坏？**
A: 不会。整个过程不写任何固件区域，最坏情况是"载入了一个不合心意的配置"，再载入别的就行。

**Q: GUI 双击打开时带一个黑色控制台窗口？**
A: 这是刻意的。CLI 模式必须有 stdout，而一个 exe 只能有一个子系统，所以用控制台子系统打包。
那个黑框正好当详细日志看；不想要的话用下面 `--noconsole` 打包（代价：命令行模式没输出）。

## 打包单文件 exe（GUI + CLI 同一个文件）

```bash
# 一键（推荐）
python build.py

# 或者手动
pip install pyinstaller
pyinstaller --onefile --name asrock_profile --console asrock_profile.py
```

产物：`dist/asrock_profile.exe`

| 运行方式 | 行为 |
|---|---|
| 双击 / 不加参数 | 打开图形界面（控制台窗口同时出现，当日志用） |
| `asrock_profile.exe info x` | 命令行模式 |

> 一个 exe 只能绑定一个子系统，所以这里选 **`--console`**：
> CLI 必须有 stdout，GUI 反而无所谓多一个黑框。两条路都能用。

### Windows 控制台编码说明

Windows 的 cmd/PowerShell 默认代码页是 **cp936(GBK)**，而 `⚠ ✔ ✘ ⇒ ↔` **不在 GBK 里** ——
直接 `print` 会抛 `UnicodeEncodeError` 把程序打挂（实测 exe 上必现）。
本工具内置了编码兼容层：这些符号在 GBK 控制台里会自动降级成 `[!] [OK] [X] => <->`，
中文照常显示；在 Git Bash、管道或重定向到文件（UTF-8）时则输出原始符号。
`selftest.py` 的第 11 节就是这条的回归测试（用 `PYTHONIOENCODING=gbk` 复现）。

## 跨平台说明

工具只用标准库，Windows / Linux / macOS 都能跑。下面是平台差异，以及**已经处理掉**的那些。

| 项目 | 处理方式 |
|---|---|
| **控制台编码** | Windows cmd 默认 cp936(GBK)，而 `⚠ ✔ ✘ ⇒ ↔` 不在 GBK 里 —— 直接 `print` 会抛 `UnicodeEncodeError` **把程序打挂**。已内置兼容层：这些符号自动降级成 `[!] [OK] [X] => <->`，中文照常；Git Bash / 管道 / 重定向（UTF-8）时不降级 |
| **非中文终端** | 终端编码扛不住中文时（典型：英文 Windows 的 cp1252），启动会打一条**英文**提示告诉你怎么办（`chcp 65001` 或 `PYTHONIOENCODING=utf-8`）—— 这条提示本身必须是英文，否则在乱码环境里根本读不了 |
| **GUI 字体** | 界面全是中文。挑不到带 CJK 字形的字体时，Linux 上会整片显示成豆腐块（□□□），而 Tk 对不存在的字体名是**静默回退**、不报错。已改成按平台候选表逐个核对 `font.families()` 再选：Windows→微软雅黑 / macOS→苹方 / Linux→Noto Sans CJK |
| **GUI 主题** | Windows `vista` / macOS `aqua` / 其它 `clam` |
| **红字告警** | macOS 的 aqua 主题会忽略 ttk 的 `foreground`，红字会变黑。所以状态标签用经典 `tk.Label`（所有平台都老实听 `fg=`） |
| **窗口尺寸** | 按屏幕尺寸夹取并居中 —— 小屏 + HiDPI 缩放下写死 820×680 会超出屏幕 |
| **打开输出目录** | Windows `os.startfile` / macOS `open` / 其它 `xdg-open`，用 `subprocess` 传**参数列表**（路径含空格也不用管） |
| **文件错误** | 权限不足、目标是目录、被占用……统一捕获 `OSError` 给干净报错（`PermissionError: ...`），**不吐 traceback** |
| **打包** | `build.py` 三平台通用；Windows 产物带 `.exe`，且打包后会**跑一遍 `--version` 自检**，防止"文件被占用 → 静默留下旧产物" |
| **测试** | 没有真实档案时（Linux / macOS / CI），`selftest.py` 会自动**合成一份格式合法的档案**，结构性用例照跑 |

### 已知限制

- **输出语言只有中文**。CLI 与 GUI 的文案都没做 i18n；非中文用户目前只能靠上面那条编码提示把中文正确显示出来。
  欢迎 PR 加 `--lang en`。
- **`detect` 不挑平台** —— 它只吃字节。Linux 上 flashrom 读出来的镜像一样能分析。
- **没在真机 Linux / macOS 上验过**。上面每一条都是按平台 API 差异处理的，
  逻辑层（解析 / 检测 / 改写）完全与平台无关，但 GUI 的实际观感只在 Windows 上看过。

## 目录结构

```
asrock_profile.py    主程序 —— CLI + GUI 同一个文件（纯标准库）
selftest.py          冒烟测试：CLI、板型警告、数值自检、detect、GUI、GBK 编码回归
build.py             一键 PyInstaller 打包
HOW-IT-WORKS.md      **实现原理**：为什么菜单里没有却仍有效、为什么读回来这么难判
README.md            本文件（中英双语）
LICENSE              MIT © inevaskfthis
```

## 贡献

欢迎补充其它板型的字段偏移表 / 档案格式变体。提交 issue 时请附上：

```
asrock_profile info <你的档案>
```

以及该 BIOS 版本对应的 Setup 模块 IFR 导出的相关片段。

## 许可证

MIT © **inevaskfthis** —— 见 [LICENSE](LICENSE)。

---

# English documentation

> Want the **reasoning behind it** — why a field with no menu entry still works, and why reading the
> active value back is hard? See [HOW-IT-WORKS.md](HOW-IT-WORKS.md).

## What it is

ASRock BIOS provides **`Save User Default to USB flash drive`** / **`Load User Default from USB flash drive`**.
The exported profile embeds **a verbatim copy of the whole UEFI Setup variable**; when you load the profile,
the **firmware itself** writes that copy back into the live variable.

This tool edits selected bytes inside that copy — which **bypasses every write restriction** and reaches
settings that are "invisible in the menu but still consumed by the firmware".

> **Verified (ASRock X370 Killer SLI 10.50)**: after setting `Setup+0x190 = 03` and loading the profile,
> CPU LLC Level 3 **survives reboot**; reading the SPI back confirms new NVRAM copies carrying that value.

## Why

Many ASRock boards (especially de-featured SKUs) have had some overclocking options removed from the BIOS:

- the **menu entry** is gone, but
- the underlying **UEFI variable slot and the driver's consumption logic are still there**

Example — **X370 Killer SLI 10.50**:

| Item | Status |
|---|---|
| `Setup+0x190` CPU Load-Line Calibration | **0 references in IFR** (invisible in menu), but the value **is consumed** ✅ |
| `Setup+0x191` VDDCR_SOC Load-Line Calibration | present in the menu, works normally |

Writing NVRAM directly does not work — runtime `SetVariable` is blocked by the SMM global write lock,
and writing the whole Setup block requires passing Secure Flash.
**The built-in "user profile" feature is the clean channel:**

```
BIOS: Save User Default to USB   →  profile embeds a full copy of the Setup variable
       ↓ this tool edits one or two bytes inside that copy
BIOS: Load User Default from USB →  firmware writes it back into the live variable, persistently
```

## Requirements

- Python **3.8+**
- **Zero third-party dependencies** (stdlib only; the GUI uses `tkinter`, bundled with official Python)
- Windows / Linux / macOS

## Quick start

### GUI

```
asrock_profile                  # no arguments = launch the GUI
asrock_profile --gui            # same thing
asrock_profile gui saved_profile   # open a profile right away
```

Six panels, top to bottom:

1. **File** — open the profile exported by the BIOS
2. **Info & checks** — board, version, Setup block location; **board mismatch shows a red warning**
3. **Known fields** — read-only table, use it to confirm the offset table matches (see below)
4. **Edit** — CPU LLC / SOC LLC dropdowns (`Auto` / `Level 1..5`), plus arbitrary `offset=value` bytes
5. **Output** — output path, overwrite-or-not, **preview only**
6. **Log** — full operation log, byte-level diff, self-check result

Click **Apply & write**, then load the resulting profile in the BIOS.

### CLI

```bash
asrock_profile info pbo2-test                          # show info + current values
asrock_profile set  pbo2-test --llc 3                  # CPU Load-Line = Level 3 (0=Auto, 1..5)
asrock_profile set  pbo2-test --soc-llc 5 --byte 0x1B1=1
asrock_profile set  pbo2-test --llc 3 --dry-run        # preview only
asrock_profile get  pbo2-test cpu-llc                  # read one field
asrock_profile dump   pbo2-test setup.bin              # export raw Setup block
asrock_profile inject pbo2-test setup.bin -o newprofile
asrock_profile detect r5.bin --baseline r4.bin        # what CPU LLC is active?
```

> When shipped as a single-file exe: no arguments → GUI, subcommand → CLI. Same binary.

## Language / 语言

Both the CLI and the GUI speak **English / 中文**:

| How | Usage |
|---|---|
| Command-line option | `asrock_profile --lang en info pbo2-test` (before *or* after the subcommand) |
| Environment variable | `ASR_LANG=en` (or `zh`) |
| GUI | the "UI language" dropdown at the top — it **rebuilds the UI in place**, keeping the loaded profile, pending changes and log |
| Default | auto: `$ASR_LANG` → can the terminal render Chinese? → system locale |

**What auto-detection means in practice**: a Chinese Windows console (cp936) → Chinese;
a terminal that cannot render Chinese (English/European console, `PYTHONIOENCODING=ascii`)
→ **switches to English automatically** instead of printing a screenful of `?`.
On POSIX, `LC_ALL` / `LANG` are honoured; on Windows they are **deliberately ignored**
(Git Bash exports `en_US.UTF-8`, which is just Git's default rather than a user choice).

> Implementation: Chinese is the *source* language; in English mode `T()` looks the string up
> in the `EN` catalogue at the end of the file (keyed by the Chinese original).
> **A missing translation merely shows Chinese — it can never crash.**
> Section 15 of `selftest.py` verifies "no untranslated strings", "no dead keys",
> "every `%s`/`%d` placeholder lines up", and "no Chinese left after switching the GUI".

## Detecting the active CPU LLC (from a full SPI image)

CPU LLC has **no menu entry** on this board, so you cannot see which level is active from the BIOS UI.
Dump the SPI and let the tool analyse it:

```bash
# dump the image first (Windows, as administrator)
#   cd to wherever AFUWINx64.EXE lives (it ships with the AMI AFU package)
AFUWINX64.EXE E:\spi\r5.bin /O

# analyse
asrock_profile detect r5.bin                      # single image (inference)
asrock_profile detect r5.bin --baseline r4.bin    # with a baseline ⇒ 100% certain
asrock_profile detect r5.bin --expect cpullc-L3   # also verify "did my profile really take effect?"
```

Output:

```
=== Setup copies located (content-signature scan over the whole image) ===
  177 signature hits; 177 kept after consensus validation (diff 0-14 bytes), 0 rejected

=== Copy chains (linked via the u24 pointer in each NVAR header; tail = newest write) ===
  len   tail(newest)  head(oldest)  variants  tail seen elsewhere?  region
  113   0x07624F      0x062370      3         yes (⇒ frozen)        high [10]
   55   0x04EA56      0x04227E      12        no  (⇒ new state)     low  [11]  ← active

=== Verdict (reading the active chain's tail record @0x04EA56) ===
  Setup+0x190  CPU Load-Line Calibration              = 3 (Level 3)
  Setup+0x191  VDDCR_SOC Load-Line Calibration        = 3 (Level 3)
  Setup+0x1A6  VTT_DDR (mV)                           = 650
  Setup+0x1A8  DRAM Voltage (mV)                      = 1300

⇒ Active CPU Load-Line Calibration = 3 (Level 3)
  Cross-check: DRAM 1300 mV / VTT_DDR 650 mV ⇒ consistent with the DDR4 2:1 rule ✔
```

### Why you cannot just take the majority value

An image contains **150+ sibling copies** of the Setup variable (historical versions left behind by
NVRAM compaction), and **they do not all hold the same value** — in the image measured above,
`+0x190` was `00` in **168 copies** and `03` in **9**, yet the value actually in effect was **`03`**.
So both "take the majority" and "take the highest offset" **give the wrong answer** (the latter picks a
stale copy written by an earlier flashrom probe).

### The criterion: copy chains

Each copy's NVAR header carries a **u24 "next" pointer** (`NVAR+6`, a distance measured from the data
start), which links all copies into a **singly-linked chain**; the tail is marked by the first three
header bytes being `ff ff ff`. Records inside a chain are ordered strictly by **write time**
⇒ **the chain tail is the most recently written state**.

Picking the active chain (single image):

1. only chains with length ≥ 2 count as real banks (length-1 entries are isolated copies in the
   external-defaults area)
2. prefer the chain whose **tail payload does not occur in any other chain** — the active chain's tail
   is a brand-new state, whereas a frozen chain's tail was copied into the active chain at some point
3. then compare **the number of distinct payloads per chain** — the active chain records every state
   change (12 vs 3 in the measurement above)
4. **with `--baseline`, the decision is made from "which chain grew" — 100% certain**

Cross-validated on real reads: the same board was judged `Auto` on the image taken before the change and
`Level 3` on the image taken after, matching reality.

> ⚠ **A single image is an inference** (well-evidenced, but not a proof). For 100% certainty, pass
> `--baseline` with the previous read. The tool always prints its reasoning so you can audit it yourself.

> 💡 You can also point `detect` at a USB profile file — it will then show **the value written in the
> profile** and explicitly warn that this is not the value currently active on the board.

## ⚠️ Limitations you must know

| | |
|---|---|
| ❌ **It will not add a menu entry** | Whether an option appears is decided by the Setup module's **IFR**, unrelated to profiles. This tool changes **values**, not **entries** |
| ⚠️ **File length must stay identical** | The tool performs in-place, equal-length edits only; do not resize the file with other tools |
| ⚠️ **Board/version fields are validated by the BIOS** | The tool never touches them (`0x00` board, `0x20` version) — leave them as-is |
| ⚠️ **"No checksum" ≠ "anything goes"** | This format really has no CRC, but writing **outside the Setup variable range** can corrupt the structure |
| ⚠️ **Loading a profile replaces all your current settings** | Correct workflow: **configure in BIOS → export profile → then edit bytes** |

## Board / version checks (the warning system)

The offset table was reverse-engineered one field at a time on **X370 Killer SLI / Gaming K4 10.50**.
On another board or BIOS version the Setup variable layout **can be completely different** — writing fixed
offsets there is pure guesswork. So the tool **speaks up**:

| Case | Behaviour |
|---|---|
| Board not in the known table | CLI `info` / `set` prints **`[版型警告]`**; GUI shows a red warning and **asks for confirmation before writing** |
| Board known, version unknown | Same (same board, different version is usually fine, but the layout may have shifted) |
| Empty board field / non-printable header | Reported as an **error** — treated as "not an ASRock profile" |

**Plus a board-independent sanity check**: on DDR4, `VTT_DDR` should be about half of `DRAM Voltage`.
The tool computes the 2:1 relation between `Setup+0x1A6` and `Setup+0x1A8` and, if it fails,
**tells you not to proceed** — this catches the "right board field, wrong offset table" case.

To verify by hand, just read these two lines:

```
Setup+0x1A8  DRAM Voltage (mV)   = 1300     ← a plausible DIMM voltage (1200–1500 typical)
Setup+0x1A6  VTT_DDR (mV)        = 650      ← should be about half of the line above
```

## Supported fields

Reverse-engineered on **X370 Killer SLI / X370 Gaming K4 10.50** (offsets inside the `Setup` variable):

| Offset | Name |
|---|---|
| `0x18B` | CPU Vcore Voltage mode |
| `0x18C` | CPU Vcore Fixed Voltage (mV) |
| **`0x190`** | **CPU Load-Line Calibration** ← hidden in the menu on Killer, but the value works |
| **`0x191`** | **VDDCR_SOC Load-Line Calibration** |
| `0x194` | CPU Vcore Offset Voltage |
| `0x196` | VDDCR_SOC Voltage mode |
| `0x197` | VDDCR_SOC Fixed Voltage (mV) |
| `0x19B` | VDDCR_SOC Offset Voltage |
| `0x1A4` | VDDP (mV) |
| `0x1A6` | VTT_DDR (mV) |
| `0x1A8` | DRAM Voltage (mV) |
| `0x1AA` | 1.05V_PROM Voltage (mV) |
| `0x1AC` | CPU VDD 1.8 Voltage (mV) |
| `0x1AE` | 2.50V_PROM Voltage (mV) |
| `0x1B0` | VPPM |
| `0x1B1` | VDDCR SOC Voltage |
| `0x1B2` | Vcore Offset Voltage |
| `0x233` | CPU Frequency and Voltage(VID) Change |
| `0x262` | SoC/Uncore OC Mode |

LLC values: `0` = Auto, `1..5` = Level 1..5.
Any unknown byte can be edited with `--byte 0xNNN=VAL` (use the "custom byte" row in the GUI).

Field aliases: `cpu-llc` / `soc-llc` / `vddcr-soc-voltage` / `vcore-offset` / `vddp` / `vtt-ddr` /
`dram-voltage` / `vpmm` / `soc-uncore-oc-mode`; a hex offset (`0x190`) also works.

## Profile format (for porting to other boards/versions)

Measured on ASRock X370 Killer SLI 10.50:

| File offset | Content |
|---|---|
| `0x00` | Board field, ASCII, **fixed 32 bytes** (`"A1818"`) |
| `0x20` | BIOS version field, ASCII, **fixed 32 bytes** (`" 10.50"`) |
| `0x38` | `02 00` |
| `0x3A` | `u32` profile body length (`0xD62` = 3426) |
| `0x55` | `u32` **Setup variable length prefix** (`0x280`) |
| `0x59`–`0x2D8` | **the full 640-byte Setup variable** |
| `0x2D9`–`0xDB3` | rest of the profile body (other variable data) |
| after | zero padding up to a fixed file length (`61,525` bytes) |

**No CRC / checksum** (crc32 and sum32 over many ranges were tried; there is no checksum field in the header).

**➡ Therefore: `file offset = Setup variable start + VarOffset`** (here: `0x59 + 0x190 = 0x1E9`)

The tool **auto-detects** the Setup variable location instead of hard-coding `0x59`, using two
structural constraints:

1. **the length prefix must be preceded by 3 zero bytes** — this eliminates every false candidate
   that lies *inside* the Setup data itself;
2. **pick the smallest offset** — Setup is the first length-prefixed blob in the profile body.

> Why "a u32 within a plausible range" is not enough: the Setup data *itself* contains sequences like
> `00 01 00 00` (= 0x100), producing a dozen false candidates in practice. And because Setup data has a
> very high ratio of tiny bytes (89–99%), **ranking by similarity puts the false ones first**.
> The two structural constraints above are what actually nail it.

If detection fails, pass `--setup-offset` / `--setup-size` manually.

## Safety / rollback

- The tool **only modifies one file on your USB drive** — it never touches the BIOS ROM or the flash chip.
- It **never overwrites the input by default** (output is `<input>.mod`); use `--inplace` or the GUI
  checkbox to overwrite.
- Every write is followed by an **automatic self-check**: the output is re-parsed to confirm
  - the file length is identical to the source,
  - **only** the bytes you specified changed,
  - each target byte really holds the target value.
- **Rollback**: in the BIOS, `Load User Default from USB` and pick the original profile you exported
  before editing.

## FAQ

**Q: The option still doesn't show up in the BIOS after loading.**
A: That's expected. Entries are decided by the IFR, which this tool never touches. You select the level
by choosing which profile to load — e.g. prepare `llc-L1` … `llc-L5` up front.

**Q: Does it support my board?**
A: As long as your BIOS has `Save/Load User Default to/from USB flash drive` and the profile parses
correctly with `info`, yes. An unknown board is fine too (the tool warns you — verify the field values
first). Paste the `info` output into an issue and I'll extend the field table.

**Q: Can it brick my board?**
A: No. Nothing firmware-related is written; the worst case is loading a configuration you didn't want,
and you can simply load a different one.

**Q: Why does the GUI come with a black console window?**
A: By design. CLI mode needs stdout, and an exe can only bind one subsystem, so it's built as a console
app. That window doubles as the verbose log. If you don't want it, build with `--noconsole` below
(trade-off: the CLI mode produces no output).

## Building a single-file exe (GUI + CLI in one binary)

```bash
# one command (recommended)
python build.py

# or manually
pip install pyinstaller
pyinstaller --onefile --name asrock_profile --console asrock_profile.py
```

Output: `dist/asrock_profile.exe`

| How you run it | Behaviour |
|---|---|
| Double-click / no arguments | Opens the GUI (the console window appears too, useful as a log) |
| `asrock_profile.exe info x` | CLI mode |

> One exe can only bind a single subsystem, hence **`--console`**: the CLI needs stdout, while the GUI
> doesn't mind an extra window. Both paths work.

### About the Windows console encoding

The default console code page on Windows is **cp936 (GBK)**, and `⚠ ✔ ✘ ⇒ ↔` are **not in GBK** —
printing them raises `UnicodeEncodeError` and kills the program (reproducible on the built exe).
The tool ships an encoding compatibility layer: on a GBK console those symbols degrade to
`[!] [OK] [X] => <->` while Chinese still renders normally; on Git Bash, pipes or redirection to a
file (UTF-8) the original symbols are emitted. Section 11 of `selftest.py` is the regression test
for this (it reproduces the environment with `PYTHONIOENCODING=gbk`).

## Cross-platform notes

The tool uses the standard library only and runs on Windows / Linux / macOS. Below is what differs per
platform, and what has **already been handled**.

| Item | How it is handled |
|---|---|
| **Console encoding** | Windows cmd defaults to cp936 (GBK), and `⚠ ✔ ✘ ⇒ ↔` are not in GBK — a plain `print` raises `UnicodeEncodeError` and **kills the program**. A compatibility layer degrades those symbols to `[!] [OK] [X] => <->` while Chinese still renders; on Git Bash, pipes or redirection (UTF-8) nothing is degraded |
| **Non-CJK terminals** | When the terminal encoding cannot represent Chinese (typically cp1252 on an English Windows), a **plain-ASCII English** hint is printed at startup telling you what to do (`chcp 65001` or `PYTHONIOENCODING=utf-8`) — the hint has to be English, otherwise it would be unreadable in exactly the situation that triggers it |
| **GUI fonts** | The whole UI is Chinese. Without a CJK-capable font it renders as tofu boxes (□□□) on Linux, and Tk **silently falls back** on unknown font names without erroring. The code now checks `font.families()` against a per-platform preference list: Windows→Microsoft YaHei / macOS→PingFang SC / Linux→Noto Sans CJK |
| **GUI theme** | Windows `vista` / macOS `aqua` / otherwise `clam` |
| **Coloured warnings** | macOS aqua ignores ttk's `foreground`, turning red warnings black. Status labels therefore use the classic `tk.Label`, which honours `fg=` everywhere |
| **Window sizing** | Clamped to the screen size and centred — a hard-coded 820×680 overflows small HiDPI laptops |
| **Opening the output folder** | Windows `os.startfile` / macOS `open` / otherwise `xdg-open`, invoked via `subprocess` with an **argument list** (paths with spaces just work) |
| **File errors** | Permission denied, target is a directory, file in use… all caught as `OSError` and reported cleanly (`PermissionError: ...`) with **no traceback** |
| **Packaging** | `build.py` works on all three platforms; the Windows artifact gets `.exe`, and after building a **`--version` self-check** runs to prevent the "file locked → old artifact silently left behind" trap |
| **Tests** | When no real profile is available (Linux / macOS / CI), `selftest.py` **synthesises a format-valid profile** so the structural tests still run |

### Known limitations

- **Output is Chinese only.** Neither the CLI nor the GUI is internationalised; for now non-Chinese users
  rely on the encoding hint above to display it correctly. PRs adding `--lang en` are welcome.
- **`detect` is platform-agnostic** — it only eats bytes. An image dumped with flashrom on Linux analyses
  exactly the same way.
- **Not verified on real Linux / macOS hardware.** Every item above is handled according to the platform
  API differences, and the logic layer (parsing / detection / patching) is platform-independent, but the
  GUI has only been visually checked on Windows.

## Project layout

```
asrock_profile.py    main program — CLI + GUI in one file (stdlib only)
selftest.py          smoke tests: CLI, board warnings, sanity check, detect, GUI, GBK regression
build.py             one-command PyInstaller packaging
HOW-IT-WORKS.md      **implementation notes** — why a hidden field still works, why detection is hard
README.md            this file (bilingual)
LICENSE              MIT © inevaskfthis
```

## Contributing

Field-offset tables for other boards and profile-format variants are welcome. For an issue, please attach:

```
asrock_profile info <your profile>
```

along with the relevant IFR excerpt of the Setup module for that BIOS version.

## License

MIT © **inevaskfthis** — see [LICENSE](LICENSE).
