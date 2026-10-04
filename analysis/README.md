# analysis/ —— 这些脚本是怎么来的

它们**不是** `asrock_profile` 的一部分，而是**当初为了搞明白 BIOS 内部结构**写的
一次性逆向分析脚本。留着是因为：里面的结论是本工具「字段偏移表」和
「检测当前 LLC」两大功能的**证据来源**，而且方法论可复用（换板型时还要用）。

跑它们需要额外数据（BIOS 镜像、解包目录），所以**不在 selftest 里**，
也不影响工具本身运行。

## 前提数据

| 需要什么 | 说明 |
|---|---|
| `C:\bios_work\r4.bin` / `r5.bin` | AFU 备份的整片 SPI 镜像（16 MB） |
| `C:\bios_work\verify_stock\stock.bin.dump\` | UEFITool 解包出的 Killer 模块树 |
| `C:\bios_work\verify_k4\k4.bin.dump\` | 同上，K4 主板（对照组） |

没有这些，脚本会 `FileNotFoundError` —— 这是预期的，它们本来就不是给普通用户跑的。

## 十二个脚本

| 脚本 | 回答什么问题 |
|---|---|
| `final_verify.py` | Killer vs K4 的四个决定性事实（模块 GUID 集合 / Setup 默认变量 / IR35201Pei / 全模块逐字节），一次算完 |
| `mod_cmp_detail.py` | 10.50 两版**电压相关模块**逐模块字节差异 —— 得出「驱动全链路等价，差异只在 Setup」 |
| `shift_cmp.py` | 固定位移下逐字节比对。**别用 `difflib.SequenceMatcher`**（540 KB 输入会被超时杀），这是替代方案 |
| `ifr_scope3.py` | IFR 门控链解析器（按缩进重建作用域栈）—— 用来证明 `Form 0x29EC` 是二选一互斥结构 |
| `offset_coverage.py` | Setup 变量每个偏移的「被谁引用」覆盖图 —— 找出 `0x190` 是零引用真空洞 |
| `retro_llc.py` | 复盘：Killer 与 K4 的 CPU LLC 到底是什么关系（含「墓碑字符串」证据） |
| `find_setup_copies.py` | 用内容特征串在整片 SPI 里定位 **所有** Setup 副本 —— ★ 工具 `detect` 的核心 |
| `r4r5_copies.py` | 逐位置对照 r4/r5 的副本值 —— 找出「档案载入新增了 8 个副本」 |
| `verify_copies.py` | 验证候选是真副本还是低熵串的巧合命中（防假阳性） |
| `voltage_state.py` | 把多张镜像的电压字节摆出来对照 |
| `nvar_enum.py` | 按真实 NVAR 条目格式枚举变量 —— 得出「278 条 / 123 个名字，无 DRAM」 |
| `find_dram_ref.py` | 全模块搜 `"DRAM"` 引用 —— 追查那个至今未解的 `QId 0x254` 门控 |

## 三条血泪教训（都写进代码注释了，这里再列一次）

1. **判 NVRAM 变量的值，绝不能只按「名字 == Setup 且 size == 0x291」筛记录。**
   同一变量有 150+ 个同源副本（嵌套 store / 变体名 / 不同 size），按名字筛会漏掉 98%。
   必须用**内容特征串全片扫描** + **结构交叉验证**（排除低熵串巧合命中）。
2. **「偏移最大 = 活体副本」是错的。** 正确做法是看哪条在两次读取之间被重写
   （NVAR 头里的 u24 指针串成单向链，**链尾 = 最新写入**）。
3. **「取多数值」也是错的。** 实测 r5 里 `+0x190` 有 168 个副本是 `00`、9 个是 `03`，
   而真正生效的是 `03`。

这三条已经内建进 `asrock_profile.py detect` 的实现里了。
