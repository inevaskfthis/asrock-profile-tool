# 实现原理 / How it works

> 这份文档解释**为什么这个工具能改到一个 BIOS 菜单里根本没有的设置**，以及**为什么"读回来判断当前值"这么难**。
> 全部结论都来自对 ASRock X370 Killer SLI 10.50 的逐字节实测；标了「推断」的地方就是推断，没有实证。

**English abstract.** The BIOS menu entry for CPU Load-Line Calibration was removed from this board, but the
underlying UEFI Setup variable field is still consumed by the firmware. ASRock's
*Save/Load User Default to/from USB flash drive* feature embeds a verbatim copy of the 640-byte Setup
variable inside a file on the USB stick, and loading it makes the **firmware itself** write that copy into
NVRAM — bypassing both the SMM runtime variable write-lock and the Secure Flash write-time signature check.
Detecting the *currently effective* value requires rebuilding the singly-linked chain that NVRAM copies form
(a u24 "next" pointer in each NVAR header), because neither the majority value nor the highest-offset copy
is correct.

---

## 一、问题：菜单里没有条目，但值仍然有效

Killer 10.50 的 Setup 模块 IFR 里，`VarOffset 0x190`
（`CPU Load-Line Calibration`）**一次引用都没有** —— 连"无 Prompt 的隐藏占位声明"都没有。
从菜单里看，这个字段在这个板子上**根本不存在**。

但把它改掉却真的生效。原因是这两件事本来就是分开的：

| | 决定什么 | 在哪 |
|---|---|---|
| **菜单里有没有这一项** | Setup 模块的 **IFR**（question 声明 + VarOffset 引用） | 只在 `Setup` 这个 FFS 模块里 |
| **值有没有人消费** | 驱动按 **VarOffset** 去读 Setup 变量 | 在 VRM / AOD / CBS 等驱动模块里 |

**实测证据**：把 `Setup+0x190` 改成 `03` 并载入后，
CPU LLC Level 3 **重启后依然有效** ⇒ 这个字段确实被后端消费。

**后端在不在 Killer 上？在。** 把 Killer 10.50 与 Gaming K4 10.50 做全模块逐字节对比：

| 模块 | 结果 |
|---|---|
| 模块 GUID 集合 | 各 549 个，**差集 0/0** |
| `IR35201Pei`（Infineon IR35201 多相 VRM 驱动） | **只差 8 字节，每处都是 −1** ⇒ 纯 TE 重定位，逻辑等价 |
| 19 个模块 | **逐字节完全相同**（含 `AodSetupDxe`、4×`CbsSetupDxe*`、`AmdPbsSetupDxe`、`CpuPei`…） |
| 真实内容差异 | **只有 4 个**：`GnbPei`（内存拓扑）、`NCT3933Pei`（Super I/O）、`NvramPei`（NVRAM 布局）、`Setup`（菜单） |

⇒ **问题从来不是"驱动被删了"，只是菜单条目没了。**

---

## 二、为什么不能直接写

三条常规路线，全部实测关闭：

| 路线 | 拦在哪 | 实测 |
|---|---|---|
| **AFU 刷写 / Instant Flash**（全片或 `/P` `/CAPSULE` `/N`） | **刷写期**签名校验（Secure Flash） | 全关 |
| **运行期 `SetVariable`**（UEFI Shell / OS 下） | **SMM 全局变量写锁** | 全部 `Security Violation` |
| **flashrom 直写 SPI**（Linux，内核直驱 FCH SPI MMIO） | 不触发 SMI，**能写** | ✅ 可用，但要有 Linux 环境 + 4KB 扇区级小心 |

Secure Flash 的拦截点是 `0x94F078` 处的 FFS，内含 `WIN_CERTIFICATE_UEFI_GUID` + 两个
RSA2048-SHA256 签名块；签名区域表在 `0x94FEC4`（20×0x18，17 条有效）。
**实测**：`[3]`（`0x9700C0–0xE00000`，BIOS 主体）进校验、`[4]` 不进。
结论是 **写入集合 == 校验集合**，不存在"可写但不被校验"的窗口；签名摘要也无法离线复现。

---

## 三、能走通的那条路：U 盘配置档案

ASRock 的 BIOS 提供 **`Save User Default to USB flash drive`** /
**`Load User Default from USB flash drive`**。

### 档案格式（实测）

| 文件偏移 | 内容 |
|---|---|
| `0x00` | 板型 ASCII，**32 字节定长**（`"A1818"`） |
| `0x20` | BIOS 版本 ASCII，**32 字节定长**（`" 10.50"`） |
| `0x38` | `02 00` |
| `0x3A` | `u32` 档案体长度（`0xD62`） |
| `0x55` | `u32` Setup 变量长度前缀（`0x280`） |
| **`0x59`–`0x2D8`** | **完整的 640 字节 Setup 变量原样副本** |
| `0x2D9`–`0xDB3` | 档案体其余（其他变量） |
| 之后 | 全零填充到固定长度 61,525 字节 |

**头部没有 CRC / 校验和**（crc32、sum32 × 多种范围都试过）⇒ 可以原地改字节。

### 为什么这条能通

```
运行期外部 SetVariable  → 被 SMM 写锁拦          （外部访问路径）
整片刷写                → 被 Secure Flash 拦      （刷写期校验路径）
载入用户配置档案        → 固件自己在 SMM 环境里写  （固件内部路径）
```

**载入档案不是"刷写"，也不是"运行期外部访问"** —— 它是固件内部组件（Setup / ASRock profile 驱动）
调用自己的变量服务去写 NVRAM。上面两道关卡在这条路径上都不适用。
（这一段是**机制推断**；实证事实是：载入后值确实写入 NVRAM 且跨重启持久。）

**实测证据**：载入 `0x190 = 03` 的档案后读回 SPI，镜像里出现了
**8 个此前不存在、且 `+0x190` 与 `+0x191` 都是 `03`** 的 Setup 副本
（`0x04B695`、`0x04BD4C`、`0x04BFD6`、`0x04C2A5`、`0x04C52F`、`0x04DD0F`、`0x04E7CC`、`0x04EA56`），
结构交叉验证通过（与已知真副本仅差 5–9 字节）。

### 代价

**BIOS 菜单里依然不会出现这个条目。** 菜单由 IFR 决定，档案只能改「值」。
换档位的方式是**换一个档案载入**。

---

## 四、读回检测：为什么"当前值"这么难判

每次读回的 16 MB 镜像里，Setup 变量有 **150+ 个同源副本**
（NVRAM compaction 留下的历史版本 + 嵌套 store + 变体名 + 不同 size），
**而且它们的值并不一样**。

### 三个错判据（都实测翻过车）

| 判据 | 会给出 | 为什么错 |
|---|---|---|
| **取多数值** | `Auto` | 实测 `+0x190`：168 个副本是 `00`、9 个是 `03`，**真值是 `03`** |
| **取偏移最大的副本** | `03` | ❌ **纯属巧合** —— 那是三周前手工 flashrom 写进去的**陈旧副本**，在另一个时间点读的镜像上它会判错 |
| **按记录名 + size 筛** | 只找到 4 条 | 其余 173 条名字为空或不同 ⇒ 会漏掉 98% 的副本 |

### 正确结构：副本是一条**单向链**

每个副本的 NVAR 记录头里有一个 **u24「下一条」指针**（位于 `NVAR+6`），
值是**从本记录数据起点到下一个副本数据起点的字节距离**：

```
数据起点 0x04B695，头 = b7 06 00  ⇒  0x04B695 + 0x6B7 = 0x04BD4C   ← 正好是下一条 ✓
数据起点 0x04DA6F，头 = a0 02 00  ⇒  0x04DA6F + 0x2A0 = 0x04DD0F   ← 正好是下一条 ✓
数据起点 0x04EA56，头 = ff ff ff  ⇒  链尾
```

- **链尾标志 = 头前 3 字节 `ff ff ff`**
- **链内记录严格按写入顺序排列** ⇒ **链尾 = 最近一次写入的状态**
- 指针自校验命中率：**147/157（r4）**、**166/177（r5）**（落空的少数是散落在外部默认区的孤立副本）

> ⚠ 记录头长度**不是统一的**：命名记录是 `NVAR + u16 size + u24 + 1B + 名字 + NUL + 数据`，
> 这批空名副本是 `NVAR + u16 size + u24 + 1B + 数据`（数据起点 = `NVAR+10`）。
> **别按 NVAR 偏移算数据起点**，最稳的锚点是**内容特征串**（Setup payload 前 16 字节），命中点就是数据起点。

### 活跃链的识别（单张镜像）

一个镜像里会有两条长链（两个 bank）：

| 镜像 | 链长 | 链尾（最新） | 链头（最旧） | 变体数 | 尾是否在别处出现 | 区域 |
|---|---|---|---|---|---|---|
| r4 | 113 | `0x07624F` | `0x062370` | 3 | 是（冻结） | 高区 [10] |
| **r4** | **36** | **`0x04945E`** | `0x04227E` | 6 | 否（**活跃**） | 低区 [11] |
| r5 | 113 | `0x07624F` | `0x062370` | 3 | 是（冻结） | 高区 [10] |
| **r5** | **55** | **`0x04EA56`** | `0x04227E` | 12 | 否（**活跃**） | 低区 [11] |

判定顺序：

1. 链长 ≥ 2 才算真 bank（长度为 1 的是散落副本）
2. **优先「链尾内容在别的链里找不到」的那条** —— 活跃链的尾是**全新状态**；
   冻结链的尾当初被抄进过活跃链，所以一定能找到
3. 再比「链内 payload 变体数」（实测 **12 vs 3**）
4. **给了基线就直接用「哪条链增长了」判定 ⇒ 100% 确定**

**交叉验证**：活跃链 r4→r5 **从 36 条长到 55 条**，冻结链 113 条**一条没动**；
链尾的值 **r4 = `Auto`、r5 = `Level 3`**，与真实现状完全一致。

### 加固手段

- **共识副本剔除巧合命中**：16 字节特征串是低熵的，用「逐字节众数」造一个典型副本，
  差异 ≤ 120 字节才算真副本（实测 177 处命中的差异全在 **0–14 字节**，零误判）
- **DRAM / VTT_DDR 2:1 交叉核对**：DDR4 下 `VTT_DDR ≈ DRAM Voltage / 2`
  （实测 1300 mV / 650 mV），这条与板型无关，能兜住"整条链认错"的情况

### 基线模式（最可靠）

```python
new  = sorted(set(copies_now) - set(copies_baseline))   # 两次读取之间新增的副本
live = new[-1]                                          # 写进去的必然是偏移最大的
val  = data[live + 0x190]
```

实测 r4→r5 新增 20 处、**0 处消失**（⇒ NVRAM 是**纯追加写**，记录从不搬家），
新增副本里偏移最大的 `0x04EA56` 就是当前值。

---

## 五、诚实的边界

- **单张镜像的活跃链判定是「推断」**，不是数学证明。证据充分（体量、变体数、尾唯一性三重），
  但想要 100% 确定就加 `--baseline`。工具会把判定依据打印出来供人工复核。
- **为什么 168 个 `00` 副本还在**：NVRAM 是追加日志，旧版本不会被删除。
  链里 `03` 与 `00` 交替出现，说明有**不同的写入者**（档案载入路径写全量副本、
  BIOS Setup 自己写它认识的那个快照）。工具不去猜每个写入者是谁，只看**写入顺序**。
- **档案载入能绕开那两个关卡**是机制推断；实证事实是"载入后值确实落进 NVRAM 并跨重启持久"。
- **菜单条目拿不回来**（除非改 IFR + 写签名区 + 有编程器兜底）。
  本工具解决的是「值」，不是「条目」。

---

## 附录 A：Killer 的 CPU LLC 与 K4 的 CPU LLC 是什么关系

**一句话**：**是同一个变量里的同一个字段**。ASRock 在 Killer 上删掉的只有「菜单那一层」。

### 三层拆开看

| 层级 | X370 Gaming K4 10.50 | X370 Killer SLI 10.50 |
|---|---|---|
| **变量定义** | 出厂默认 `Setup` 变量 **640 B**，VarStoreId `0x1`，GUID `EC87D643-EBA4-4BB5-A1E5-3F3E36B20DA9` | **同一份定义**，逐字节只差 **2 处**：`+0xB3: 01/02`、`+0xB4: 00/02`（与电压无关） |
| **字段定义** | `VarOffset 0x190` · `Size 8` · `Min 0` · `Max 5` · `Flags 0x10` | **完全相同** |
| **菜单条目** | `OneOf` question，`QuestionId 0xF9`，6 个选项 `Auto` + `Level 1..5` | **`VarOffset 0x190` 零引用**（连隐藏占位都没有） |

⇒ 两版的电压字段布局**完全一致**，`0x190` 在两板上就是同一个字段，不是"碰巧同偏移的两个不同字段"。

### K4 那条 question 的完整定义（IFR 原文）

```
0x2AE01: OneOf Prompt: "CPU Load-Line Calibration{p200#Level %d",
                Help: "FFSGUID:0951C12E-7D7E-49c9-93AA-593E8135904A",
                QuestionFlags: 0x10, QuestionId: 0xF9,
                VarStoreId: 0x1, VarOffset: 0x190,
                Flags: 0x10, Size: 8, Min: 0x0, Max: 0x5, Step: 0x0
                { 05 91 D7 03 D8 03 F9 00 01 00 90 01 10 10 00 05 00 }
0x2AE12:   OneOfOption Option: "Auto"    Value: 0, Default, MfgDefault
0x2AE19:   OneOfOption Option: "Level 1" Value: 1
           … Level 2 / 3 / 4 / 5 (Value 2..5)
```
外层门控：`GrayOutIf` + `EqIdVal QuestionId: 0x206, Value: 0x1`
（`QId 0x206` = VarStoreId `0x5`「AMICallback」`+0x00`）

对照 Killer 的 SOC LLC：
```
0x2AEA0: OneOf Prompt: "VDDCR_SOC Load-Line Calibration…", QuestionId: 0xF7,
                VarStoreId: 0x1, VarOffset: 0x191, Size: 8, Min 0, Max 5
                { 05 91 E0 03 E1 03 F7 00 01 00 91 01 10 10 00 05 00 }
```
**opcode 形状完全同型**（`05 91 <prompt_id> <help_id> <qid> 00 01 00 <voffset> 01 10 10 00 05 00`），
说明两板来自**同一套 VFR 源**，Killer 只是把这条 question 从源码里删了。

### ⚠ 陷阱：两板的 QId 完全不可比

| VarOffset | Killer 10.50 | K4 10.50 |
|---|---|---|
| `0x190` | **无 question** | CPU Load-Line Calibration（QId **0xF9**） |
| `0x191` | VDDCR_SOC LLC（QId **0xF7**） | VDDCR_SOC LLC（QId **0xFB**） |
| `0x1B1` | VDDCR SOC Voltage（QId **0xF9**） | **无** |
| `0x1B2` | Vcore Offset Voltage（QId **0xF8**） | **无** |

**同一个 QId `0xF9` 在两板上指的不是同一个条目** —— 两版 IFR 是各自独立编译的
（Killer 从 `0xF7` 起连续编号，K4 从 `0xF8`/`0xF9` 起）。
⇒ **任何"按 QId 移植"的思路都是错的。**

### 物证：Killer 里那条 question 的「墓碑」

Killer 的 Setup 模块里，**那条 question 的两条字符串原封不动地留着**：

| | K4 | Killer |
|---|---|---|
| Prompt 串 `CPU Load-Line Calibration{p200#Level %d` | `@0x3BBD8` | **`@0x3BC95`（孤儿）** |
| Help 串 `FFSGUID:0951C12E-7D7E-49c9-93AA-593E8135904A` | `@0x3BC45` | **`@0x3BD02`（孤儿）** |
| `OneOf` 本体 | `@0x2AE01` | **不存在** |

⇒ ASRock 是**把 question 从 VFR 源码里删掉后重新编译**，字符串池没清理干净。
这条比"IFR 里零引用"更能说明问题 —— 它证明**这条 question 曾经存在过**。

> 💡 顺带解开一个旧误会：`0951C12E-7D7E-49C9-93AA-593E8135904A` **不是模块 GUID**，
> 而是 ASRock 塞进这条 question 的 `Help` 字段里的 **FFSGUID 标签**。
> 所以之前在 16 MB 里用四种字节序 + 三种文本编码都搜不到它 —— 它本来就只是个逻辑标签。

### 后端：两版逐字节等价

| 检查 | 结果 |
|---|---|
| 模块 GUID 集合 | 各 **549** 个，**差集 0 / 0** |
| `IR35201Pei`（Infineon IR35201 多相 VRM 驱动） | 两版都是 **4288 B**，**只差 8 处且每处都是 ±1**（`BD→BC`、`CE→CD`×5、`BF→BE`）⇒ 纯 TE 重定位，**逻辑等价**；两版都含 Setup 变量描述符 GUID 字节序列 |
| `Setup` 模块 body | Killer 541,088 B / K4 **540,928 B**（差 +160）；位移 **−189** 下 **347,924 B（64.3%）连续相同**，其中 `0x030BFD..0x083B01` 单段就有 **339,716 B** |

⇒ 差异**全部集中在 IFR 与字符串区**（Killer 的 `0x023AF9` 之前那一带），
代码段与其余菜单基本一致。

### 所以为什么不能"把 K4 的 question 复制到 Killer"

1. **QId 会撞车**：Killer 已占用 `0xF7/0xF8/0xF9/0xFA`（含义还和 K4 不同）
2. **门控不同**：K4 的灰化门是 `QId 0x206`（VarStoreId `0x5`「AMICallback」），
   Killer 是 `QId 0x204`（VarStoreId `0x4`「SystemAccess」）—— **不是同一条字节**
3. **字符串池要重算** ID 偏移，牵一发动全身
4. 最要命的是：**改 Setup 模块 = 写签名区 `[3]` = 要有编程器兜底**

⇒ 所以我们最后**没有走这条路**，改用 U 盘配置档案直接改**值**，把「菜单层」整个绕开了。
（真要走 IFR 路线，本仓库早先做的 `verify/always2_setup.bin` 是最优版本：
新加的 question **只受 `GrayOutIf 0x204==1`** 约束，不受 Killer 那张表单的 DRAM 二选一影响。）

### 没搞明白的一点（如实标注）

K4 这条 question 的 `Help` 字段填的是 `FFSGUID:0951C12E-…`，
而它旁边的 SOC LLC 填的是正常的人话帮助文本（"…helps prevent VDDCR_SOC voltage droop…"）。
**为什么这条是 FFSGUID、不是人话，没有证据，不下结论。**
可能是这版 VFR 源码里帮助文本为空、构建工具用 FFSGUID 占位；也可能是 ASRock 特意加的标记。
