# LabVIEW Native MCP

让 AI 通过 [MCP](https://modelcontextprotocol.io) 直接操作 LabVIEW：在真实的前面板和程序框图上放控件、建循环、写公式、连线、整理、保存、运行，并把结果截图返回给 AI 检查。

底层是 LabVIEW 自带的 VI Scripting（经 COM 调用），不注入 DLL，也不模拟鼠标。

<p align="center">
  <img src="docs/images/pid_diagram.png" width="58%">
  <img src="docs/images/pid_panel.png" width="40%">
</p>
<p align="center"><sub>PID 闭环：从空 VI 开始全部由 MCP 搭建，4 个移位寄存器、27 根连线，运行结果与 Python 仿真逐点一致。</sub></p>

> **项目状态：公开测试中。** 目前只在 **LabVIEW 2021 x64** 上做过验收。欢迎试用、提 issue；需要适配其他 LabVIEW 版本的实验室或团队，欢迎联系合作。

## 安装

**前置条件**：Windows x64，LabVIEW 2021 x64 开发版（Runtime Engine 不够），Python 3.12 x64（python.org 版，不是 Microsoft Store 版）。不需要 npm。

**最省事的方式：让你的 AI 来装。** 把下面这段话发给 Claude Code / Codex / Cursor 等能执行命令的 AI：

```text
请按照 https://github.com/chinayuren2022-2025/labview-native-mcp/blob/main/docs/INSTALL_FOR_AI.md
的步骤，在这台 Windows 电脑上安装 LabVIEW Native MCP，并把它注册到你自己（当前这个 AI 客户端）的 MCP 配置里。
每一步都检查输出，遇到前置条件不满足就停下来告诉我。
```

**手动安装**：

```powershell
git clone https://github.com/chinayuren2022-2025/labview-native-mcp.git
powershell -NoProfile -ExecutionPolicy Bypass -File .\labview-native-mcp\install.ps1
```

安装器会创建独立的 `.venv`（位于 `%LOCALAPPDATA%\LabVIEWNativeMCP`），自动找到 LabVIEW，并真的连上 LabVIEW 建一个测试 VI。看到 `INSTALL_PROBE_OK` 就说明装好了。安装器同时生成 `mcp-config.json`，把其中的 `labview-native` 一项合并进你的 MCP 客户端配置即可，各客户端的写法见 [docs/INSTALL_FOR_AI.md](docs/INSTALL_FOR_AI.md)。

## 示例

两个示例都从空 VI 开始，全部通过 MCP 调用完成：搭建、整理、保存、运行，再和 Python 的计算结果逐点比对。

```powershell
python examples\pid_loop.py       # PID 闭环 + 二阶对象，阶跃响应曲线
python examples\signal_chain.py   # 正弦生成 → 增益/偏置 → 限幅 → 统计
```

<p align="center">
  <img src="docs/images/signal_diagram.png" width="58%">
  <img src="docs/images/signal_panel.png" width="40%">
</p>

用安装目录里的 Python 运行：`%LOCALAPPDATA%\LabVIEWNativeMCP\.venv\Scripts\python.exe`。输出在 `examples/output/`。

## 工具

| 工具 | 作用 |
|---|---|
| `search_styles` | 按名字搜索 LabVIEW 原生对象（共 1117 种），支持模糊匹配 |
| `new_vi` / `open_vi` | 新建 VI（不覆盖已有文件）/ 打开 VI |
| `add_object` | 放置控件、函数、结构，或按路径放置 SubVI |
| `inspect_node` | 列出节点的端子；循环会标出 `i` / `N` / `condition`，常量标出 `value` |
| `add_shift_register` | 给循环加移位寄存器，返回 `initial` / `previous` / `next` / `final` 四个端子 |
| `wire` | 连接两个端子，跨结构时自动生成隧道 |
| `get_property` / `set_property` / `invoke` | 调用任意原生属性和方法（例如 Formula 的 `FormExpr`、Build Array 扩展输入） |
| `find_objects` | 按类名重新查找对象 |
| `clean_up_diagram` | 调用 LabVIEW 自带的整理框图 |
| `connector` | 设置连接器图案并分配端子 |
| `save_vi` / `run_vi` | 保存；同步运行并读取输出 |
| `render_vi` | 导出前面板或程序框图的 PNG 给 AI 看 |
| `select_polymorphic` / `configure_express_parameters` | 切换多态实例，写入 Express VI 参数 |

推荐的调用顺序（服务器也会把它告诉 AI）：`search_styles` → `new_vi` → `add_object` → 配置 → `inspect_node` → `wire` → `clean_up_diagram` → `save_vi` → `run_vi` / `render_vi`。

数据格式：普通 JSON 列表是数组；簇写成 `{"type": "cluster", "value": [...]}`；U8 数组写成 `{"type": "u8_array", "value": [1, 2, 255]}`（也支持 `u16` / `u32` / `i32` / `double`）。

## 已知限制

- 只在 LabVIEW 2021 x64 上验收过；其他版本可能能用，但没有验证。
- `render_vi` 导出的**多曲线**波形图，曲线可能在某个长度处被截断（运行数据本身是完整的）。原因还没查明，欢迎有经验的朋友帮忙。
- `run_vi` 是同步执行，会真的运行 VI，包括硬件和网络 I/O；不适合无限循环的 VI。请只在可信的客户端里使用，不要把它暴露成网络服务。
- 引用只在当前 LabVIEW 会话里有效；改多态、切换控件/显示控件之后需要重新获取端子。
- 并不保证覆盖所有节点和数据类型。遇到不支持的情况，错误信息会带上 LabVIEW 的原始错误码和参数提示，欢迎贴到 issue。

## 测试

```powershell
python tests\acceptance.py   # CRC-16 VI 从零搭建 + 标准向量、多态、Express、Case、属性节点
python tests\boundary.py     # 错误恢复、覆盖保护、数组/簇、并发、嵌套循环
python tests\usability.py    # 对象搜索、诊断信息、反向连线、整理、循环与移位寄存器
```

测试只会创建临时 VI，不碰你的工程和硬件。

## 参与测试与合作

- 试用中遇到问题、想要的功能，直接提 [issue](https://github.com/chinayuren2022-2025/labview-native-mcp/issues)，最好附上 AI 的调用记录和 LabVIEW 报错。
- 需要 LabVIEW 2021 以外的版本（2018–2025 等），或想在自己的实验室项目里落地，欢迎联系合作。

## 许可证

MIT。`work/lvscript/` 是 [Erdos Miller LV-Scripting](https://github.com/erdosmiller/lv-scripting)（MIT）的副本，为兼容 LabVIEW 2021 和 COM 调用做了修改，原许可证见 `work/lvscript/LICENSE.txt`。本仓库不包含任何 NI 软件，使用者需要自己拥有 LabVIEW 授权。
