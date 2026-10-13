# 安装指南（写给执行安装的 AI）

你是一个能在用户 Windows 电脑上执行 PowerShell 命令的 AI 助手。按顺序完成下面 5 步，把 LabVIEW Native MCP 安装好并注册到**你自己所在的 MCP 客户端**。

规则：

- 每条命令执行后都检查输出。某一步写着「停下」时，把原因告诉用户，不要绕过。
- 不需要 npm，也不要用 npm / npx 安装。
- 不要修改系统级执行策略，不要关闭杀毒软件，不需要管理员权限（第 5 步排障里明确说明的除外）。
- 修改任何客户端配置文件前先备份，只添加或替换 `labview-native` 这一项，保留其他内容。

---

## 第 1 步：检查前置条件

```powershell
[Environment]::Is64BitOperatingSystem
py -3.12 -c "import sys, struct; print(sys.executable, struct.calcsize('P') * 8)"
Get-ChildItem "C:\Program Files\National Instruments" -Directory -Filter "LabVIEW 20*" | Select-Object Name
```

期望：

- 第一行是 `True`。
- 第二行打印 Python 3.12 的 `python.exe` 路径和 `64`。如果 `py` 不存在，再试 `& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -V`。
- 第三行列出了 `LabVIEW 2021`。

处理方法：

- **没有 Python 3.12 x64**：停下，告诉用户需要安装。可以建议用户执行 `winget install -e --id Python.Python.3.12`，或从 python.org 下载。得到用户同意后才能代为安装。路径里带 `WindowsApps` 的是 Microsoft Store 的占位程序，不能用。
- **没有 LabVIEW 2021**：停下。告诉用户本工具目前只验收了 LabVIEW 2021 x64 开发版；其他版本请联系作者合作适配。
- **LabVIEW 2021 装在别的位置**：记下它的完整路径，第 3 步用 `-LabVIEWHome` 传入。

## 第 2 步：获取源码

安装器会把运行所需的文件复制到 `%LOCALAPPDATA%\LabVIEWNativeMCP`。源码目录本身留着，用来跑示例和测试，所以放在用户目录下。

**有 git：**

```powershell
git clone https://github.com/chinayuren2022-2025/labview-native-mcp.git "$env:USERPROFILE\labview-native-mcp"
$src = "$env:USERPROFILE\labview-native-mcp"
```

**没有 git（下载 zip）：**

```powershell
$zip = "$env:TEMP\labview-native-mcp.zip"
Invoke-WebRequest https://github.com/chinayuren2022-2025/labview-native-mcp/archive/refs/heads/main.zip -OutFile $zip
Expand-Archive $zip -DestinationPath "$env:USERPROFILE" -Force
$src = "$env:USERPROFILE\labview-native-mcp-main"
```

如果目录已经存在，说明以前装过。用 `git -C $src pull` 更新，或者删掉后重新下载。删除前先问用户。

## 第 3 步：运行安装器

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$src\install.ps1"
```

如果需要，可以加这些参数：

- `-LabVIEWHome "D:\NI\LabVIEW 2021"`：LabVIEW 不在默认位置时使用。
- `-Python "C:\path\python.exe"`：`py -3.12` 找不到 Python 时使用。
- `-InstallDir "D:\tools\LabVIEWNativeMCP"`：不想装在默认目录时使用。

安装器会做下面几件事：

1. 创建独立的 `.venv`，安装 `mcp` 和 `pywin32`。pip 走用户本机的源配置；国内网络慢时，可以先 `$env:PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"` 再运行。
2. 找到 LabVIEW。
3. 启动 LabVIEW（首次大约 30–60 秒），建一个临时 VI 做真实测试。

**成功的标志是输出里有 `INSTALL_PROBE_OK`**，最后还会打印 `mcp-config.json` 的内容。没有出现这一行，就算安装失败，见第 5 步排障。

读出注册所需的三个值，后面要用：

```powershell
$cfg = (Get-Content "$env:LOCALAPPDATA\LabVIEWNativeMCP\mcp-config.json" -Raw | ConvertFrom-Json).mcpServers.'labview-native'
$py = $cfg.command; $server = $cfg.args[0]; $lvhome = $cfg.env.LABVIEW_HOME
"$py`n$server`n$lvhome"
```

（用了 `-InstallDir` 的话，把路径换成对应目录。）

## 第 4 步：注册到 MCP 客户端

只注册到你自己所在的客户端；用户另外要求时，再注册其他客户端。

### Claude Code

```powershell
claude mcp add labview-native --scope user -e "LABVIEW_HOME=$lvhome" -- $py $server
claude mcp list
```

`claude mcp list` 里 `labview-native` 显示 connected 即可。

### Codex CLI

```powershell
codex mcp add labview-native --env "LABVIEW_HOME=$lvhome" -- $py $server
```

首次调用会启动 LabVIEW，比较慢。建议在 `~\.codex\config.toml` 的 `[mcp_servers.labview-native]` 下加一行 `tool_timeout_sec = 180`。

### 使用 JSON 配置文件的客户端

| 客户端 | 配置文件 | 顶层键 |
|---|---|---|
| Cursor | `%USERPROFILE%\.cursor\mcp.json` | `mcpServers` |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` | `mcpServers` |
| Windsurf | `%USERPROFILE%\.codeium\windsurf\mcp_config.json` | `mcpServers` |
| VS Code（Copilot） | 工作区 `.vscode\mcp.json` | `servers`，并且条目里加 `"type": "stdio"` |
| Cline / 其他 | 客户端设置里的 MCP 配置文件 | 一般是 `mcpServers` |

操作步骤：

1. 文件存在时先备份：`Copy-Item $file "$file.bak"`。
2. 用你的文件编辑能力在顶层键下加入（或替换）这一项，值用第 3 步读出的实际路径，反斜杠写成 `/`：

```json
"labview-native": {
  "command": "C:/Users/<用户名>/AppData/Local/LabVIEWNativeMCP/.venv/Scripts/python.exe",
  "args": ["C:/Users/<用户名>/AppData/Local/LabVIEWNativeMCP/tools/labview_mcp.py"],
  "env": { "LABVIEW_HOME": "C:/Program Files/National Instruments/LabVIEW 2021" }
}
```

3. 改完确认文件仍是合法 JSON（`Get-Content $file -Raw | ConvertFrom-Json | Out-Null` 不报错）。如果原文件里有注释（JSONC），手动编辑，不要用脚本整体重写。
4. 告诉用户重启客户端或开一个新会话，新配置才会生效。

## 第 5 步：验证与排障

告诉用户：在**新会话**里让 AI 调用一次 `search_styles`，参数 `query="While Loop"`。返回结果里应该有 `While Loop #1`。也可以直接让 AI「新建一个 VI，放一个加法函数，连上两个输入和一个输出，运行看看 1+2」。

不经过客户端、只验证服务本身：

```powershell
& $py "$env:LOCALAPPDATA\LabVIEWNativeMCP\tools\install_probe.py"
```

| 现象 | 处理 |
|---|---|
| `Python 3.12 x64 not found` | 第 1 步没装好 Python；或者用 `-Python` 传入 `python.exe` 的完整路径 |
| `Cannot locate a usable LabVIEW Development System` | 用 `-LabVIEWHome` 指定 LabVIEW 2021 的安装目录；只装了 Run-Time 不行 |
| `Connect LabVIEW.Application` 失败 / `Invalid class string` | 手动打开一次 LabVIEW 2021。仍然失败时，请用户在**管理员** PowerShell 执行 `& "C:\Program Files\National Instruments\LabVIEW 2021\LabVIEW.exe" /RegServer` 重新注册 COM |
| 装了多个 LabVIEW 版本，结果打开了别的版本 | 先手动启动 LabVIEW 2021 再调用；必要时按上一行重新注册 2021 的 COM |
| pip 下载失败或很慢 | 设置 `PIP_INDEX_URL` 为国内镜像后重跑第 3 步，安装器可以重复执行 |
| 客户端里看不到工具，或首次调用超时 | 确认配置里的路径存在；重启客户端；先手动打开 LabVIEW 再调用；在客户端里调大 MCP 工具超时 |
| LabVIEW 弹出对话框（许可证、恢复文件等）导致卡住 | 请用户在 LabVIEW 里关掉对话框后重试 |

最后向用户汇报：安装目录、`INSTALL_PROBE_OK` 是否出现、注册到了哪个客户端，以及还需要用户做的事（例如重启客户端）。
