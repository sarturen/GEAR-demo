# GEAR 测试台

以**被测设备（DUT）为中心**的 Windows 硬件自动化测试台。把继电器、串口、ADB、摄像头抽象成被测设备的具名资源，用例只引用逻辑名，不出现 COM 口和板卡编号。

---

## 目录

- [快速开始](#快速开始)
- [GUI 入口](#gui-入口)
- [三层模型](#三层模型)
- [配置：settings.yaml](#配置settingsyaml)
- [写用例](#写用例)
- [画面检测](#画面检测)
- [界面速览](#界面速览)
- [移植到另一台架](#移植到另一台架)
- [二次开发](#二次开发)
- [测试](#测试)
- [已知边界](#已知边界)

---

## 快速开始

```bash
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m geartest
```

或直接双击 **`run.bat`**（它会检查虚拟环境并在缺失时给出提示）。

需要 Python **3.12**。系统里如果只有 3.14 装不了 PySide6，所以固定用 3.12。

依赖：PySide6、opencv-python、numpy、pyserial、paramiko、PyYAML。

---

## GUI 入口

```
run.bat
  └─ .venv\Scripts\python.exe -m geartest
       └─ geartest/__main__.py          ← 三行，转发到 app.main()
            └─ geartest/app.py : main()  ← 真正的入口
                 ├─ QApplication + 设置字体（含中文字形回退）
                 ├─ load_settings()      ← 读 config/settings.yaml
                 ├─ MainWindow(...)      ← geartest/ui/main_window.py
                 └─ app.exec()
```

`MainWindow` 建一个 `Bench`（设备注册表）和一个 `Recorder`（录制器），然后挂八个面板：

| 面板 | 文件 | 作用 |
|---|---|---|
| 被测设备 | `ui/panels/dut_panel.py` | 拓扑、adb 序列号绑定、一键拉起整台设备 |
| 继电器 | `ui/panels/relay_panel.py` | 多块板同时在线 + 具名电源资源 |
| 串口 | `ui/panels/serial_panel.py` | 合并视图 + 每口一个标签页 |
| ADB | `ui/panels/adb_panel.py` | 按设备跑命令、logcat 流式监控 |
| SSH 容器 | `ui/panels/ssh_panel.py` | 远端容器执行、SFTP |
| 监控画面 | `ui/panels/camera_panel.py` | 屏幕网格、ROI 拖拽、异常事件表 |
| 测试流程 | `ui/panels/flow_panel.py` | 加载/运行用例、录制、步骤状态 |
| 配置 | `ui/panels/settings_panel.py` | 增删改整份 bench 定义，写回 settings.yaml |

想在代码里嵌进别的程序，只要 `from geartest.ui.main_window import MainWindow`，先建 `QApplication` 再传 `Settings` 进去。

---

## 三层模型

分清这三层是理解整个工具的关键：

```
物理设备  relays[] / consoles[] / cameras[] / ssh[]
             ↓  被引用
逻辑资源  relay_channels[] / screens[]        ← 用例只认这一层的名字
             ↓  被组织
被测设备  devices[]                          ← 用例声明它跑在哪台设备上
```

- **物理设备**：机器相关，换台架就要改。COM 口号、摄像头索引、SSH 主机。
- **逻辑资源**：接线拓扑，进版本库、跟用例一起评审。`KL30` 是 1 号板的第 9 路；`屏幕2` 是 5 号摄像头的某块区域。
- **被测设备**：把一组资源归到一台 DUT 名下。成员关系**显式声明**，不做推断。

一个例子：

```yaml
relay_channels:
  - {name: KL15, relay: relay1, channel: 8}   # 逻辑名 → 物理位置
  - {name: KL30, relay: relay1, channel: 9}
devices:
  - name: box1
    adb_serial: "0123456789"
    power: [KL15, KL30]
    consoles: [SOC, MCU]
    screens: [屏幕1, 屏幕2, 屏幕3]
```

用例里写 `resource: KL30`，引擎解析成"relay1 的第 9 路"。把电源挪到另一块板，只改 `relay_channels` 一行，所有用例不动。

资源解析失败分两种，因为修法不同：

```
继电器资源「KL31」已定义，但没有绑定到设备「box1」。该设备声明的继电器资源：KL15, KL30
继电器资源「KL99」未定义。已定义：KL15, KL30, KL31
```

---

## 配置：settings.yaml

文件在 `config/settings.yaml`。**可以不写 YAML**——「配置」页把下面七类全部做成了可增删改的界面，保存时校验并写回本文件。手写也可以，两种方式等价。

程序退出时会整体重写本文件，手写注释不保留（顶部有生成头部说明字段含义）。端口留空时不会打开任何设备。

### relays[] — 物理继电器板

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | `relay1` | 逻辑名 |
| `port` | `''` | 例如 `COM4` |
| `baudrate` | `9600` | |
| `slave_id` | `1` | Modbus 从站地址 |
| `coil_base` | `0` | 第 1 路对应的线圈地址。**有的板子从 1 开始**，接上真板先试 1 路确认 |
| `timeout` | `1.0` | 响应超时（秒） |
| `inter_frame_delay` | `0.03` | 帧间延时 |
| `pulse_ms` | `500` | 脉冲默认时长 |

### relay_channels[] — 具名继电器资源

| 字段 | 说明 |
|---|---|
| `name` | 用例里引用的名字，例如 `KL30` |
| `relay` | 属于哪块板 |
| `channel` | 1–16 |
| `pulse_ms` | 该路的脉冲默认时长 |
| `description` | 备注，界面上显示 |

### consoles[] — 物理串口

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | `SOC` | 用例里引用的名字 |
| `port` | `''` | 例如 `COM35` |
| `baudrate` | `115200` | |
| `bytesize` / `parity` / `stopbits` | `8` / `N` / `1` | |
| `encoding` | `utf-8` | 解码用 |

### cameras[] — 物理摄像头

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | `cam4` | 逻辑名 |
| `source` | `'0'` | 采集索引 `0`/`1`/…，或 `rtsp://` / `http://` 地址 |
| `width` / `height` | `1280` / `720` | 请求的分辨率 |

### screens[] — 具名监控区域（屏幕 = 摄像头 + ROI）

屏幕和摄像头是分开的：屏幕本质是"某摄像头的某块区域"，一个摄像头里可以看两块屏。

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | `屏幕1` | 用例里引用的名字 |
| `camera` | `''` | 用哪个摄像头 |
| `roi` | 整幅 | `{x, y, w, h}`，**归一化比例**，界面上拖拽框选 |
| `sensitivity` | `50` | 检测强度总旋钮，见下文 |
| `freeze_timeout_s` | `10.0` | 画面静止多久算冻屏 |
| `black_confirm_s` | `2.0` | 持续多久算黑屏（短于此的是闪黑） |
| `min_transient_ms` | `80` | 短于此的黑屏视为采集毛刺，不报事件 |
| `transient_max_s` | `1.5` | 黑屏与闪黑的分界（仅记录用） |

### devices[] — 被测设备

| 字段 | 说明 |
|---|---|
| `name` | 用例里 `device:` 引用的名字 |
| `adb_serial` | adb 序列号。**物理属性，在界面里绑定**；留空则用 adb 默认设备 |
| `adb_path` | `adb` 可执行文件 |
| `power` / `consoles` / `screens` | 该设备拥有哪些资源（逻辑名列表） |
| `description` | 备注 |

### ssh[]

| 字段 | 说明 |
|---|---|
| `name` / `host` / `port` / `username` / `password` / `key_file` | 连接信息 |
| `workdir` | 代码工作路径，命令会先 `cd` 到这里 |
| `docker_exec` | 进容器前缀。**必须自带 shell**，例如 `docker exec -i ctr sh -c` |

SSH 不参与设备资源表，用例里按名字全局引用。

---

## 写用例

放在 `flows/`，YAML。最小骨架：

```yaml
name: 用例名
device: box1            # 必填：按 name 或 adb_serial 解析
stop_on_failure: true   # 默认 true

on_failure:             # 任一步失败后执行，然后结束
  - snapshot: {screens: all}
  - adb_logs: {}

steps:
  - relay: {resource: KL30, action: "on"}
  - adb_wait: {timeout: 60}
  - expect_screen: {screen: 屏幕2, state: lit, timeout: 20}
```

> YAML 1.1 里不带引号的 `on` / `off` 会被解析成布尔值。引擎两种都认，但**建议写成 `"on"` / `"off"`** 更清楚。

### 步骤一览

| 步骤 | 参数 | 说明 |
|---|---|---|
| `log` | 字符串 | 只记录一行 |
| `mark` | 名字 | 记下当前时刻，供后面 `since` 回看 |
| `wait` | 秒数 | 可被停止 |
| `relay` | `{resource, action, pulse_ms}` | `action`: `on` / `off` / `pulse` / `read`。**省略 `resource` 时** `action` 可以是 `all_on` / `all_off` / `read`，作用于**本设备声明的**电源，不是整块板 |
| `serial_write` | `{console, text}` 或 `{console, hex}` | `\r\n` 要自己写 |
| `serial_expect` | `{console, contains\|regex\|expect, absent, timeout, since}` | `contains` 按字面匹配，`regex`/`expect` 按正则 |
| `watch` | `{id, console, regex\|contains}` | 后台守护，全程记录命中 |
| `expect_watch` | `{id, at_most, at_least}` | 判定守护命中次数 |
| `adb` | `{command, expect…, timeout}` | 跑命令并断言输出 |
| `adb_wait` | `{timeout}` | 等设备枚举**且 shell 真能应答** |
| `ssh` | `{host, command, expect…, timeout}` | `host` 只有一个时可省 |
| `expect_screen` | `{screen, state, timeout, since}` | 见下文 |
| `expect_screen` | `{screen, sequence: [dark, lit], timeout, since}` | 先变黑再复亮，单步完成 |
| `repeat` | `{times, name, steps}` | 循环；内层失败会报出轮次和步号 |
| `snapshot` | `{screens: all\|[名字], note}` | 存档当前帧 + 证据环 + 串口尾部 |
| `adb_logs` | `{commands, timeout}` | 拉 adb 输出到证据目录 |

每个步骤都可以另带 `on_failure: [...]`，在流程级处理之前先跑。

### `since`：从什么时候开始看

| 值 | 含义 |
|---|---|
| `step`（默认） | 上一步结束之后。写指令再等回应时用这个，既不漏掉间隙里的回复，也不会匹配到更早的输出 |
| `iteration` | 本轮循环开始之后 |
| `flow` | 整个流程开始之后 |
| `<mark 名字>` | 某个 `mark` 步骤记下的时刻 |

`since` 是几个关键字容易踩坑的地方。典型场景——**跨多步的一次观察**：

```yaml
- mark: cycle
- relay: {resource: KL15, action: "off"}
- wait: 5
- relay: {resource: KL15, action: "on"}
- expect_screen:
    screen: 屏幕2
    sequence: [dark, lit]
    timeout: 20
    since: cycle          # 从下电之前开始看
```

锚点必须落在**下电之前**：`sequence` 要覆盖的是"下电→变黑→等待→上电→复亮"整段。这也是 `sequence` 存在的理由——写成两个连续的 `expect_screen` 会有监视空档：前一步一看到变黑就返回，后一步还没开始，而快面板可能在这间隙里变黑又恢复。

### 后台守护：为什么不能用逐步断言

复位横幅往往在继电器恢复后 1–3 秒才打印，落在任何紧凑断言的窗口之外；轮询式断言只在它自己的步骤运行期间看；而且会丢掉到达时间戳。

```yaml
- watch: {id: mcu_reset, console: MCU, regex: "reset|reboot|panic|watchdog"}
- watch: {id: soc_panic, console: SOC, regex: "Kernel panic|BUG:|reboot"}

- repeat: {times: 20, name: KL15 上下电, steps: [...]}

- expect_watch: {id: mcu_reset, at_most: 0}
- expect_watch: {id: soc_panic, at_most: 0}
```

命中会带上时间和原始行，失败信息里直接给出首次命中的时刻和内容。

### 失败证据

失败时写一个目录：`evidence/<时间戳>_<设备>_<流程名>/`

```
summary.txt          流程、设备、开始时间、解析后的物理绑定、守护命中统计
screens/             各屏幕当前帧 + 证据环（闪黑只有 100~300ms，事后存当前帧没有意义）
  console_*.txt      串口尾部
adb/                 adb 命令输出
```

`summary.txt` 里的**解析后物理绑定**是关键——用例说的是 `KL30` 和 `屏幕2`，第二天来排查的人需要直接看到那意味着"板卡 relay1 第 9 路"和"摄像头 cam5 源 5 ROI x=…"，而不用去翻配置文件。

---

## 画面检测

### 判据

全部建立在**仿射不变量**上，没有任何绝对亮度阈值。亮度变化（背光、环境光、自动曝光）在 ROI 上近似 `g' = a·g + b`，两条规则由此而来：

**1. `norm = (g − median(g)) / max(std(g), 1)` 对该变换严格不变**

用来做变化检测（冻屏）。所以背光漂移产生的 `norm` 差异为零、不会重置冻结计时器；反过来，亮度闪烁也不会被当成画面变化。

**2. 与常数比较的量必须是分子分母同增益的比值**

- **平坦度** = 高通后 P95−P5 ÷ 噪声底。两项都随增益缩放，比值无量纲。真正平坦的画面（黑屏、纯色）落在 3.3 附近（高斯噪声 P95−P5 = 3.29σ），有内容的画面高几个数量级。
- **暗度** = max(R,G,B) 的 P95 与**自动学习的亮度基线**比较。用 max 而不是亮度，是因为蓝色"无信号"底亮度只有 29、会被亮度判据误判成黑屏。
- **基线**只在不平坦的帧上更新，取 60 秒窗口的 P90。黑屏因此无法把自己拖下水；长时间黑暗后样本过期会主动作废并重新校准。

**冻屏**用 20×15 的分块活动图：内容块里有多大比例在最近 2 秒内动过。这样"静态菜单 + 光标闪烁"不会被误判冻屏（只有光标那些块是内容块，它们一直在动），而"画面卡死 + 时钟跳动"仍然能报（时钟只占少数块，活动比例塌陷）。

### 屏幕状态

| `state` | 含义 |
|---|---|
| `lit` | 窗口内**不得**出现任何变黑（亮屏保持） |
| `dark` | 窗口内**必须**出现变黑（含闪黑） |
| `black` | 窗口内**必须**出现持续黑屏 |
| `freeze` / `no_freeze` | 必须 / 不得出现冻屏 |
| `no_black` | 不得出现黑屏或闪黑 |

`sequence: [dark, lit]` 表示先变黑再复亮。

### 一个重要限制

**`lit` 在亮度基线未建立时会直接判失败**，而不是通过。

因为检测器在拿到基线前无法判定"暗"，如果按"没出现黑屏事件"来判，一块从未上电的屏幕会顺利通过"确认点亮"的检查。基线需要约 20 帧有内容的画面才能建立，所以流程启动时会先把该设备的所有屏幕都开起来。

代价是：**"背光关闭"和"亮着但显示黑色画面"在画面上无法区分**，两者都是平坦且暗。失败信息会如实写成"没有出现可判定的画面内容"，并给出平坦度和亮度读数，不做过度断言。

### sensitivity 调节

唯一的强度旋钮，0–100（默认 50），同时缩放三个常数：

| | 0 | 50 | 100 |
|---|---|---|---|
| 平坦度阈值 `k_flat` | 3.0 | 6.0 | 9.0 |
| 暗度比 `r_dark` | 0.29 | 0.45 | 0.61 |
| 分块变化阈值 `k_block` | 4.9 | 3.5 | 2.1 |

它是无量纲的，**不接触任何亮度数值**。如果你需要调一个"亮度阈值"，说明算法里有项写错了——请提出来。

---

## 界面速览

- **被测设备**：左边选设备，中间是解析后的绑定（`KL30 → 板卡 relay1 第 8 路`），下面是实时状态和启停日志。「拉起整台设备」会一次开好串口、屏幕、ADB、继电器——**先把所有问题收集齐再报**，而不是碰到第一个就死，同时继电器最后开。
- **继电器**：每块板一行 16 个小按钮纵向堆叠，多块板同时可见；下面是具名电源资源（`KL15` / `KL30`）及状态。
- **串口**：默认「合并」标签页，所有已打开串口按端口分色汇总；每个口另有自己的标签页和发送框。**输出成块刷入**——CH340 在 115200 下一个口每秒能吐几千行，逐行刷会卡死界面。
- **监控画面**：所有屏幕网格平铺、同时可见，每块可拖拽自己的 ROI；共用一个预览定时器（检测本身在各采集线程上全速跑，预览只是显示）。
- **测试流程**：步骤表 + 分色日志；勾上「录制手动操作」后，在其它面板上的动作会转成流程步骤，可导出为 YAML 回放。
- **配置**：七个分区各一个子标签页（被测设备 / 继电器板 / 继电器资源 / 串口 / 摄像头 / 监控屏幕 / SSH 主机），表格 + 新增/编辑/删除/上下移动。编辑先落在**工作副本**上，点「保存并应用」才校验、写盘、并按新配置重建所有面板——**不需要重启**。

  校验会把问题一次列全并标红，例如跨引用：

  ```
  · 继电器资源[KL15]：relay「relay9」在 relays 里不存在（现有：relay1）
  · 被测设备[box1] 的 power 引用了不存在的「KL99」（现有：KL15、KL30）
  ```

  有问题的配置**不会被写盘**。删除一个仍被引用的资源前会先提示影响面（哪个设备/哪块屏在用它）。
  
---

## 移植到另一台架

按这个顺序做，每一步都能单独验证：

**1. 改物理绑定（换机器只改这里）**

在「配置」页对应分区里改，或直接改 `config/settings.yaml` 的 `relays[].port`、`consoles[].port`、`cameras[].source`、`devices[].adb_serial`。改完点「保存并应用」，面板会按新配置重建。

**2. 改拓扑（接线变了才改）**

「配置」页里改 `relay_channels` / `screens` 的名字、路数、所属板卡和摄像头。**用例不用动。**

**3. 对线圈基址**

接上真板后先只操作 1 路，确认 `coil_base` 是 0 还是 1。这个值错了会静默操作错误的通道。

**4. 校准 ROI**

在「监控画面」页对着实时画面拖拽框住屏幕有效区域。存的是归一化比例，换分辨率或换摄像头都不失效。

**5. 看一遍实时读数**

首次接上真实摄像头时，盯一会儿实时状态里的「平坦度 / 亮度 / 噪声底」三个读数确认量级：正常画面平坦度应当是几十到几百，黑屏在 3 左右。数值不对通常是 ROI 没框对或镜头没对准。

**6. 用不涉及硬件的用例先跑通**

`flows/分辨率切换抗闪黑回归.yaml` 这类改一下就能验证串口链路。

---

## 二次开发

### 加一个配置字段

改两处，界面自动跟上：

1. `config.py` 的 dataclass 加字段
2. `config_schema.py` 的对应 `Section.fields` 加一条 `Field(...)`（标签、`kind`、范围）

`test_config_schema.py` 里的覆盖率测试会**双向**检查这两个列表一致——少写一条就红。字段类型决定控件：`text` / `int` / `float` / `bool` / `choice`（候选下拉）/ `multichoice`（复选列表）/ `roi`。

### 加一种设备

1. `config.py` 加 dataclass，挂到 `Settings` 上（未知键检查会自动要求它出现），并在 `config_schema.py` 加一个 `Section`
2. `devices/` 加驱动，用 `LinePump`（`devices/stream.py`）做流式输出
3. `bench.py` 加懒加载的 open/close 和资源解析
4. `ui/panels/` 加面板，在 `main_window.py` 注册标签页，并给它一个 `rebuild()`

`rebuild()` 是「保存并应用」调用的——面板必须能从 `bench.settings` 重新读取自己，否则改配置就得重启。

### 加一个流程步骤

在 `geartest/flow/engine.py` 的 `_handlers` 字典里注册一个方法：

```python
def _step_mything(self, spec) -> str:
    device = self._require_device()          # 已解析的 DUT
    board, channel = self.bench.power(device.name, spec["resource"])
    ...
    return "成功信息"                         # 失败抛 StepFailure("原因")
```

再补上 `_label()` 里的显示名。步骤拿到的是原始 spec，`self._since(spec)` 可解析 `since`，`self._incident_dir()` 可拿证据目录。

**别在步骤里读 `settings.yaml`**——用 `self.bench.resolve(device, kind, name)`，它同时校验成员关系。

### 改检测算法

三个文件，职责分开：

- `vision/invariants.py` — 纯统计。`analyse()` 一帧出所有量。**没有摄像头和 Qt 依赖**，可以脱离一切单独测。
- `vision/detector.py` — 状态机。`process(frame, t) -> list[Event]`，无副作用、无 IO。
- `vision/monitor.py` — 线程与生命周期。

改判据请**先写合成场景测试**（`tests/synthetic.py` 有帧生成器），再动代码。`tests/test_vision.py` 里最重要的一组是**跨亮度一致性**：同一个场景在四种亮度设置下重放，事件类型和时间戳必须完全一致。改动破坏了亮度不变性时，它会机械地抓出来。

---

## 测试

```bash
.venv\Scripts\python tests\run_all.py
```

五套，共 98 项，**都不需要硬件**：

| 套件 | 项数 | 内容 |
|---|---|---|
| `test_vision.py` | 18 | 合成视频驱动检测器：黑屏、泛灰黑屏（各亮度）、蓝色无信号底不误报、亮度渐变不误报且不掩盖冻屏、100/150/300ms 闪黑、单帧损坏不误报、采集停滞与冻屏区分 |
| `test_relay_modbus.py` | 17 | 桩串口逐字节验证 Modbus 帧、位序、异常码、CRC、超时、脉冲复位 |
| `test_flow_steps.py` | 20 | 引擎行为：资源解析的两种错误、`all_off` 只动本设备、循环、守护、`on_failure` 不覆盖原始原因、**未校准的 `lit` 必须失败** |
| `test_config_schema.py` | 23 | 配置元数据完整性（见下）、交叉引用校验、取值范围、存读往返 |
| `test_gui_smoke.py` | 20 | 离屏建窗、配置编辑器的编辑/保存/拒绝非法配置、保存后重建面板、录制、ROI 换算、关闭路径 |

单独跑某一套：`.venv\Scripts\python tests\test_vision.py`

---

## 已知边界

**没有任何物理设备被验证过。** 写这套工具的环境上没有继电器板、CH340、adb 设备和摄像头。具体地说：

- Modbus 对过标准的报文向量和桩串口，但**没和真板子通过话**。首次上板先试 1 路，确认 `coil_base`。
- 画面检测跑的是合成视频，**没见过真实摄像头**。最没把握的两点是真实相机的自动曝光速度，以及 PWM 背光频闪——后者会与相机帧率拍频、产生滚动亮度带，可能干扰平坦度判据。
- ADB 和 SSH 只有结构性验证（没设备、没主机可连）。`adb_wait` 的"shell 真能应答"逻辑也没在真机上跑过。
- 界面只做过离屏冒烟测试，**布局是靠渲染截图人眼看的**，没有自动化视觉回归。

另外两个设计上的取舍，不是缺陷但值得知道：

- **不自动断电。** 用例失败后继电器保持原状，因为切断电源可能毁掉现场。要收尾请在 `on_failure` 里显式写 `relay: {action: all_off}`。
- **`all_off` 只作用于本设备声明的通道**，不是整块板——两块 DUT 共用一块板时，A 的收尾不能断掉 B 的电。
