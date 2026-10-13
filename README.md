# LabVIEW Native MCP

Windows / LabVIEW 2021 的原生 VI Scripting MCP 服务。Python COM 调用 LabVIEW worker VI，在真正的前面板和框图上创建、配置、连线、保存和运行；不使用 DLL 注入。

## 安装

需要 Windows x64、合法安装的 LabVIEW 2021 x64 Development System（启用 VI Scripting）、Python 3.12 x64。LabVIEW Runtime Engine 不够。本仓库不包含 NI 软件、帮助文件或驱动。

AI 可执行安装（先克隆或下载本仓库，不需要 npm 账号）：

```powershell
git clone https://github.com/chinayuren2022-2025/labview-native-mcp.git
powershell -NoProfile -ExecutionPolicy Bypass -File ./labview-native-mcp/install.ps1
```

`install.ps1` 自动查找真正的 Python 3.12 x64，检查 LabVIEW 安装，将发行输入安装到 `%LOCALAPPDATA%/LabVIEWNativeMCP`，创建专用 `.venv`，安装依赖，并建立真实 MCP 会话创建/保存一个原生 Add 节点。成功时打印 `INSTALL_PROBE_OK` 和生成的 `mcp-config.json`。此 JSON 是客户端配置片段，AI 应按客户端格式合并 `mcpServers.labview-native`，保留原有配置并备份；安装器不覆盖用户客户端配置。

可传 `-Python C:/path/python.exe`、`-LabVIEWHome C:/path/LabVIEW`、`-InstallDir C:/path/install`。`-SkipProbe` 只跳过真实操作检查，不能据此宣称已验证。无需管理员权限，不修改系统 Python，不安装/启用 LabVIEW，不修改系统执行策略（Bypass 仅此次 PowerShell 进程）。依赖下载遵循本机 pip 源配置。首次探测可能启动 LabVIEW，产生临时 Probe.vi，但不会运行用户 VI 或连接硬件。

必须使用实际 Python 安装路径；WindowsApps 的 python alias 被明确拒绝。LabVIEW/Python 缺失时退出并说明前置条件，不静默下载安装。服务工具缓存位于可写安装目录，安装器不复制旧 worker 缓存。

LabVIEW 路径按 `LABVIEW_HOME` → Windows 注册表（两种 registry view）→ NI 安装目录发现，并检查 Development System 的实际 Scripting 资源；安装器复用同一发现逻辑，不再固定 2021 路径。多版本共存时建议显式指定 `LABVIEW_HOME`，COM 注册必须指向同一版本。路径发现不是跨版本兼容性证明，目前仍只验收 LabVIEW 2021。

### MCP 客户端配置

将下列内容合并到客户端的 MCP 配置，替换两处绝对路径。该示例不自动覆盖客户端配置。

```json
{
  "mcpServers": {
    "labview-native": {
      "command": "C:/path/to/Python312/python.exe",
      "args": ["C:/path/to/labview-native-mcp/tools/labview_mcp.py"],
      "env": {"LABVIEW_HOME": "C:/Program Files/National Instruments/LabVIEW 2021"}
    }
  }
}
```

直接运行 `python tools/labview_mcp.py` 会等待 stdio MCP 请求；不是交互 CLI，不应向 stdout 混入日志。

## 工具

| 工具 | 操作 |
|---|---|
| new_vi / open_vi | 创建空 VI（拒绝覆盖已有文件）或打开现有 VI |
| add_object | 创建原生 style、控件、结构或按路径加载 SubVI |
| search_styles | 搜索/分页列出实际原生 style 名和 ID，支持相近名称候选 |
| find_objects / inspect_node | 查对象、当前类、端子方向及循环 i/N/condition 端子 |
| get_property / set_property | 原生属性读取、内容和模式编辑 |
| invoke | 调用原生方法，配置事件、分支、方法选择、动态端子 |
| wire | 连接两端点，接受反向参数顺序，启用原生自动路由/结构隧道 |
| connector / save_vi | 连接器配置与保存 |
| clean_up_diagram | 显式重排对象/重路由连线，不自动保存 |
| run_vi / render_vi | 同步执行，返回指定输出或实际 PNG |
| select_polymorphic | 切换实例并返回新端子 |
| configure_express_parameters | 映射属性页参数并持久化，不重生成实现 |

操作顺序：查 style → 创建 → 配置对象类/方法/读写/实例/端子数 → 重新获取端子 → 连线 → `clean_up_diagram`（按需）→ 保存 → 运行/图像检查。保存和导图不会隐式整理布局。引用仅对当前 LabVIEW 会话有效；删除、替换、revert 后重新发现。一次服务所有 COM 请求在同一 STA 线程串行执行。

### 类型传输

- 属性返回 `{ "value": ... }`，不丢失数组形状。
- 普通列表是数组；cluster 用 `{ "type": "cluster", "value": [...] }`。
- cluster 数组用 `{ "type": "cluster_array", "value": [[...], [...]] }`。
- U8 数组用 `{ "type": "u8_array", "value": [1,2,255] }`；支持 u16/u32/i32/double 包装。
- 原生 byte SAFEARRAY/memoryview 递归转换成 JSON 整数数组。
- `get_property`/`invoke` 需使用安装版本原生短名称或唯一 ID。不是任意英文自然语言解释器。
- `search_styles(query="", limit=50, offset=0)` 返回 `{total, matches: [{name, id}]}`；空查询分页列出全部原生 style，非空查询先子串匹配，再相近候选。`add_object` 仍要求确切名字或 ID，不擅自选候选。例：`While Loop` 会提示 `While Loop #1` 和 `While Loop #2`。
- `inspect_node` 的端子保留 `reference/name/source`，新增 `native_name/role`。循环语义名为 `i`、`N`、`condition`，role 为 `iteration/count/condition`；For 的 N 使用 LoopCount Tunnel 的 Outside Term，返回可直接连线的端子。未启用的 For 条件端子不会被检查操作创建。
- `wire` 的 source/destination 是端点参数名，不强制信号方向；LabVIEW 校验实际方向/类型。For 输出隧道可能默认自动索引，需要标量结果时显式设置 `LoopTunnel.IndexMode=0`。
- COM 失败包含操作、可用的 VI 路径、HRESULT/十六进制码及 LabVIEW 描述；原生错误簇保留错误码和来源。保留 Unicode，不对已损坏字符串猜测恢复；原生字节用 Windows ANSI 解码并保留字节证据。创建已有 VI 明确提示拒绝覆盖。
- 每次导图写入独立 `work/mcp_render/<VI名>/export_*`，不会把上一张 PNG 当作本次结果；目录会保留。未经整理的重叠布局可能让正确连线看起来像直连，PNG 不能替代实际端点检查。

## 验证

```powershell
python tools/mcp_acceptance.py
python tools/mcp_boundary.py
python tools/mcp_usability.py
```

客户端建立实际 stdio MCP 会话并操作真实 LabVIEW，生成临时测试 VIs，不接触串口硬件。

`mcp_acceptance.py`：多态 U32→U16、Express Amplitude 持久化，然后从空 VI 创建 CRC，检查四个标准向量并返回 PNG；另检查控件方向、Build Array、Case Frame、PropertyItem.IsWrite。

`mcp_boundary.py`：无效引用/未知style/member恢复、已有文件覆盖拒绝、标量/空单多U8数组/cluster、并发请求、两层For Loop跨框图连线与运行、模式变更、不能执行的Formula修复。原工作区边界结果为9类通过、0失败、114请求；发布包会在干净worker缓存下再次验证。

`mcp_usability.py`：2026-10-13 真实 stdio/LabVIEW 验证通过 1117 个 style 分页/模糊搜索、候选和覆盖拒绝/COM诊断、反向参数连线、Celsius→Formula→Fahrenheit 的实际端点及 100→212 / 0→32 运行、整理前后连线拓扑不变、While/For 的语义端子与真实执行、For 条件端子启用/移除。整理前后 PNG 已视觉检查，重叠控件在整理后分离。这次既有 acceptance 和 boundary 也通过，boundary 为9类/114请求/0失败；无固定 LabVIEW 参数的隔离安装打印 INSTALL_PROBE_OK。检查只使用临时 VI，不触碰用户工程/硬件。

### 稍复杂的 VI 示例

运行 `python tools/build_signal_demo.py`，通过真实 stdio MCP 从空 VI 创建两级 For Loop 的信号处理框图：正弦采样 → 原生 Multiply/Add 做增益/偏置 → Formula 条件限幅 → 数组求和/大小/最大最小值统计。生成到唯一的 `work/signal_demo_<时间>/`，保留 VI、整理前后 PNG、运行后的前面板及 `verification.json`；结束时打开真实 VI。测试64点、17点负增益、1点负限幅值（按绝对值解释），逐点对照独立 Python 数值计算，不触碰硬件。演示输入 Samples 使用正整数；不声称支持空数组或任意无效输入。2026-10-13 实测2个 For Loop、25根 Wire、0坏线，三组通过；保存后 Revert 重新载入并再次执行通过。


## 明确边界

- 不是所有节点、类型或模式的通用支持保证。只对有限 Windows/LabVIEW 2021 场景取得证据。
- Express 参数持久化已验证；信号种类、端子结构或内部实现重生成未实现/未验收。
- 不保证所有 .NET 构造函数重载能通过原生属性选择；必要时使用无参构造后属性初始化，并单独验收行为。
- 现有控件类型不随多态签名自动迁移；调用者需重新取得端子并可能重建控件。
- 同步 Run 会等 VI 完成；无限循环控制器不适合直接调用 run_vi，客户端取消不等于 LabVIEW 已停止。
- 改图通常要求停止/未保留的 VI；错误如1055、1062、1077、1193等原生返回，不隐藏异常。
- 相对路径从服务工作目录解释；建议始终给绝对路径。
- 拒绝 `new_vi` 覆盖不等于沙箱：open/set/save 可编辑授权路径，run 可执行任意 VI，包含硬件/网络行为。仅在可信客户端和明确授权范围使用，不公开暴露为网络服务。
- 不自动注册客户端、不修改 LabVIEW.ini、不安装串口驱动、不默认触发硬件命令。

## 文件与许可证

`tools/` 是Python服务和真实客户端验证。`work/lvscript/` 是经 LabVIEW 2021 兼容修补的 Erdos Miller LV-Scripting（MIT），保留原许可证。`work/workers/`、`work/mcp_render/` 和测试VI均运行时生成，不随发行包分发。

上游：https://github.com/erdosmiller/lv-scripting 。修改：Create Invoke/Property Node 去掉不适合 COM 的旧输入/输出联接代码，保留原生 class/style，动态生成独立 worker。完整许可证见 `work/lvscript/LICENSE.txt`。用户需自行拥有 NI LabVIEW 授权。

本仓库不包含用户的 ShutterControl 工程、其生成副本、串口测试日志或第三方安装包。项目能力验证中的从空重建与硬件协议测试记录是独立学习记录，不表示本通用 MCP 已对任意项目等价验收。
