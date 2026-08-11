# GitHub Release Monitor

通过 GitHub Actions 定时检查多个 GitHub 仓库是否发布了新的 Release，并使用飞书群自定义机器人发送卡片通知。项目只依赖 Python 标准库，不需要安装依赖或运行服务器。

## 功能

- 同时监控任意数量的公开或私有 GitHub 仓库
- 可为每个仓库选择是否包含 prerelease，自动忽略 draft
- 飞书卡片展示版本、发布时间、更新说明和 Release 链接
- 支持飞书机器人的签名校验
- 通知成功后才更新状态；单个仓库失败不会阻止其他仓库检查
- 自动将状态提交回仓库，Action 重跑不会重复通知
- 支持手动强制通知，便于验证飞书机器人

> 本项目监控的是 GitHub **Releases**，仅创建 Git tag 而没有创建 Release 不会触发通知。

## 快速开始

### 1. 配置要监控的仓库

编辑 [`config/repositories.json`](config/repositories.json)：

```json
{
  "notify_on_first_run": false,
  "repositories": [
    {
      "repo": "cli/cli",
      "name": "GitHub CLI",
      "include_prereleases": false
    },
    {
      "repo": "microsoft/vscode",
      "name": "Visual Studio Code",
      "include_prereleases": true
    },
    "astral-sh/uv"
  ]
}
```

仓库可以写成简短的 `"owner/repo"` 字符串，也可以使用对象：

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `repo` | 是 | - | GitHub 仓库，格式为 `owner/repo` |
| `name` | 否 | `repo` | 飞书卡片中显示的名称 |
| `include_prereleases` | 否 | `false` | 是否把预发布版本视为新版本 |

顶层的 `notify_on_first_run` 默认为 `false`：首次检查只记录各仓库当前版本，从第二次开始通知新版本。设为 `true` 会在首次检查时通知每个仓库的当前版本。

### 2. 创建飞书机器人

1. 在目标飞书群中打开“设置 → 群机器人 → 添加机器人 → 自定义机器人”。
2. 复制机器人生成的 Webhook 地址。
3. 建议启用“签名校验”，并保存生成的签名密钥。

### 3. 添加 GitHub Actions Secrets

进入本项目的 **Settings → Secrets and variables → Actions**，添加：

| Secret | 必填 | 说明 |
| --- | --- | --- |
| `FEISHU_WEBHOOK_URL` | 是 | 飞书机器人的完整 Webhook 地址 |
| `FEISHU_WEBHOOK_SECRET` | 否 | 启用飞书签名校验时填写 |
| `MONITOR_GITHUB_TOKEN` | 否 | 访问其他私有仓库时使用的 fine-grained PAT |

监控公开仓库时，工作流会自动使用当前项目的 `github.token`，通常无需配置 `MONITOR_GITHUB_TOKEN`。监控其他私有仓库时，内置 token 没有跨仓库权限，需要创建一个只允许访问目标仓库、且具有 **Contents: Read-only** 权限的 fine-grained PAT。

不要把 Webhook 地址或 PAT 直接写进配置文件。

### 4. 允许 Action 保存状态

进入 **Settings → Actions → General → Workflow permissions**，选择 **Read and write permissions**。工作流需要把 [`state/releases.json`](state/releases.json) 提交回默认分支。

如果默认分支受保护，还需要在分支规则或 ruleset 中允许 GitHub Actions 写入；否则检测和通知能执行，但状态无法保存，可能造成重复通知。

### 5. 启用并测试

推送这些文件后，在仓库的 **Actions → Monitor GitHub releases** 页面启用工作流。选择 **Run workflow** 即可手动执行。

首次运行默认只建立版本基线。要立即测试飞书通知，手动运行时勾选 `force_notify`，工作流会为每个存在 Release 的仓库发送当前版本。

## 执行频率

工作流默认每小时整点和第 30 分钟运行，也就是每 30 分钟一次：

```yaml
- cron: "0,30 * * * *"
```

可以编辑 [`.github/workflows/monitor-releases.yml`](.github/workflows/monitor-releases.yml) 调整频率。GitHub Actions 的 cron 使用 UTC，并且在平台繁忙时可能延迟；最短调度间隔为 5 分钟。

## 本地检查

只检查配置和 GitHub Release，不发送飞书通知、也不修改状态：

```bash
python src/release_monitor.py --dry-run
```

公开仓库可匿名访问，但 GitHub API 的匿名限额较低。可以临时提供 token：

```bash
GITHUB_TOKEN=你的令牌 python src/release_monitor.py --dry-run
```

运行测试：

```bash
python -m unittest discover -s tests -v
```

## 状态与故障处理

每个仓库最后一次成功处理的 Release ID、tag 和发布时间保存在 `state/releases.json`。只有以下情况会推进状态：

- 首次建立基线；
- 飞书通知发送成功；
- 使用 `force_notify` 且通知成功。

如果 GitHub API 或某个飞书通知失败，工作流会显示失败并在下一次运行时重试该仓库；同一轮中其他成功仓库的状态仍会保存。删除某个仓库在状态文件中的条目，可以让它在下次运行时重新执行首次检查逻辑。

## 分支部署模式

如果希望公开的 `main` 分支保持为干净模板，可以把实际监控配置和状态放在 `monitor` 分支。当前工作流由默认分支的定时事件触发，但会自动 checkout `monitor`，并将 `state/releases.json` 提交回 `monitor`，不会修改 `main`。

## 项目结构

```text
.
├── .github/workflows/monitor-releases.yml  # 定时任务
├── config/repositories.json                # 仓库配置
├── src/release_monitor.py                  # 检测与通知逻辑
├── state/releases.json                     # 已处理版本状态
└── tests/test_release_monitor.py            # 单元测试
```
