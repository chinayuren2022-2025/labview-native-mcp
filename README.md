# LabVIEW Native MCP

Windows / LabVIEW 2021 的原生 VI Scripting MCP 服务。Python COM 调用 LabVIEW worker VI，在真正的前面板和框图上创建、配置、连线、保存和运行；不使用 DLL 注入。

## 安装

需要 Windows x64、合法安装的 LabVIEW 2021 x64 Development System（启用 VI Scripting）、Python 3.12 x64。LabVIEW Runtime Engine 不够。本仓库不包含 NI 软件、帮助文件或驱动。

```powershell
python -m pip install -r requirements.txt
```

必须使用实际 Python 安装路径；WindowsApps 的 python alias 可能不能工作。服务首次操作可通过 COM 启动 LabVIEW；用户会看到窗口。请将本仓库解压到可写目录，不要从 ZIP 内运行。

默认 LabVIEW 路径为 `C:\Program Files\National Instruments\LabVIEW 2021`。其他路径用环境变量 `LABVIEW_HOME`；其他 LabVIEW 版本未验收。COM 注册也必须指向要使用的 LabVIEW 版本。

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
| find_objects / inspect_node | 查对象、当前类和端子方向 |
| get_property / set_property | 原生属性读取、内容和模式编辑 |
| invoke | 调用原生方法，配置事件、分支、方法选择、动态端子 |
| wire | 连接当前端子引用 |
| connector / save_vi | 连接器配置与保存 |
| run_vi / render_vi | 同步执行，返回指定输出或实际 PNG |
| select_polymorphic | 切换实例并返回新端子 |
| configure_express_parameters | 映射属性页参数并持久化，不重生成实现 |

操作顺序：创建 → 配置对象类/方法/读写/实例/端子数 → 重新获取端子 → 连线 → 保存 → 运行/图像检查。引用仅对当前 LabVIEW 会话有效；删除、替换、revert 后重新发现。一次服务所有 COM 请求在同一 STA 线程串行执行。

### 类型传输

- 属性返回 `{ "value": ... }`，不丢失数组形状。
- 普通列表是数组；cluster 用 `{ "type": "cluster", "value": [...] }`。
- cluster 数组用 `{ "type": "cluster_array", "value": [[...], [...]] }`。
- U8 数组用 `{ "type": "u8_array", "value": [1,2,255] }`；支持 u16/u32/i32/double 包装。
- 原生 byte SAFEARRAY/memoryview 递归转换成 JSON 整数数组。
- `get_property`/`invoke` 需使用安装版本原生短名称或唯一 ID。不是任意英文自然语言解释器。

## 验证

```powershell
python tools/mcp_acceptance.py
python tools/mcp_boundary.py
```

客户端建立实际 stdio MCP 会话并操作真实 LabVIEW，生成临时测试 VIs，不接触串口硬件。

`mcp_acceptance.py`：多态 U32→U16、Express Amplitude 持久化，然后从空 VI 创建 CRC，检查四个标准向量并返回 PNG；另检查控件方向、Build Array、Case Frame、PropertyItem.IsWrite。

`mcp_boundary.py`：无效引用/未知style/member恢复、已有文件覆盖拒绝、标量/空单多U8数组/cluster、并发请求、两层For Loop跨框图连线与运行、模式变更、不能执行的Formula修复。原工作区边界结果为9类通过、0失败、114请求；发布包会在干净worker缓存下再次验证。

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
