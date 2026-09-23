---
name: auto-continue
description: 排查和配置 Auto-Continue —— Claude Code 在 Windows 上的看门狗。看懂它的活动日志（每行都带窗口 id）、判断某个会话为什么没被接上、配置模型恢复与「跑完接下一棒」、把日志脱敏后发给作者。触发词：auto-continue, 自动继续, 会话卡住没人管, 5 小时限制没恢复, 窗口没被接上, activity.log, 看门狗, watchdog, 模型恢复, after-finish, 导出日志反馈。
---

# Auto-Continue

Claude Code 的 Windows 看门狗：盯着「窗口停下来了」并替用户推下去。
单文件免安装，MIT 开源、免费、可商用。

**先读日志再下结论。** 它是屏幕抓取工具，凭记忆猜它「应该」怎么做，
十次有九次猜错——日志里写着它当时看见了什么、做了什么。

## 东西在哪

| | |
|---|---|
| 活动日志 | `%LOCALAPPDATA%\auto_continue\activity.log` |
| 轮转 | 到约 1 MB 转成 `.log.old`，只留一份；事发第二天排查记得连 `.old` 一起看 |
| 设置 | 注册表，随 GUI 保存；界面上 **Advanced…** 里全部可改 |
| 日志本体 | 只记它自己的判断和动作，**从不记录终端内容** |

```bash
tail -40 "$LOCALAPPDATA/auto_continue/activity.log"
```

## 怎么读一行日志

```
2026-09-06 07:33:31  [warn]  limit on '◑ 某会话' #093c → resets 10:40pm …
                     级别         事件            窗口标题   窗口 id
```

- **`#093c` 是窗口 id**（2.0.17 起）。标题会随对话改名、也可能重名，
  **认 id 不认标题**。同一个 id 就是同一个窗口；窗口关掉重开会换 id。
- 级别：`info` 记录 / `warn` 需要注意 / `fire` 真的发出了按键 / `err` 出错。
  **数「打了多少字」就数 `fire` 行**：一次故障里反复重发，每一次都记 `fire`
  （2.1.2 起；之前第二次往后记成 `info`，数出来会偏少）。托盘气泡只在第一次弹，
  但日志和接口事件一次不少。

### 两种「沉默」必须分清（2.0.18 起日志会直说）

```
[warn]  could not read the screen of #093c this pass — treating it as unknown, not as 'nothing there'
[info]  screen of #093c readable again
```

Windows 的 UI Automation 偶尔会拒绝一个窗口，这时它**什么都没看到**——
和「看了，屏幕上没问题」完全是两回事。见到上面这行，就别再把同一时刻的
「没检测到异常」当成「一切正常」。

## 它管四种情况

| 日志里长这样 | 它做了什么 |
|---|---|
| `network retries exhausted` / `network API error` / `response truncated mid-stream` | 每隔重试间隔发一次 `continue`，直到会话重新开始输出 |
| （屏幕上）`No response from the API after 6m · retrying once, waiting up to 10m` | **什么都不做**：这是 Claude Code 自己在重试，算「正在运行」。重试也失败、出现单独一行的 API 报错说没有响应时，才按网络错误处理 |

网络类报错**必须单独成行**才算数（2.1.3 起）。夹在一句话中间的同样文字，是有人在引用它，
通常是 Claude 在解释你贴的报错，不会被当成卡住去发 `continue`。
| `limit on … → resets …; will fire at …` | 等到重置时间 + Buffer，再发 `continue` |
| `answered the permission prompt` | 回车放行安全拦截（默认开） |
| `answered the chooser` | 回车选中默认项（**默认关**，要自己打开） |

配套的两句话：

- `network error cleared on … ; recovered` —— 会话重新开始输出了，戳有效果。
- `limit message gone on … before fire; assuming handled manually` ——
  到点前发现横幅没了（用户自己继续了），于是**放弃**发送，不多打一个 `continue`。

## 它**故意**不管的，别劝用户去「修」

1. **限制到期时那个「要不要买额外用量」的选择框** —— 另一个选项是花钱，
   回车会选中高亮项。这是钱的决定，屏幕抓取不该替人做。它只标记 `⏎ Limit prompt` 等你。
2. **登录过期** —— `OAuth token expired … can't fix this; run /login in that session`。
   打字救不回来，所以只报一次，不骚扰。**让用户去那个会话里 `/login`**。
3. **正在输出的会话** —— 到点的 `continue` 会等它安静下来再发，日志写
   `is due but the session is mid-turn; holding the 'continue' until it goes quiet`。
   这不是卡住，是刻意的。
4. **切换模型的对话框** —— 属于「模型恢复」那套流程，不会被当成普通选择框顺手回车。
5. **配套程序把自动回车关了** —— 日志 `api: settings auto_permission=off`
   （或 `auto_choose=off`）。这是本机另一个程序（例如手机上远程批准的那种）
   通过本地 API 关的，好让人来决定，Advanced 里的勾也跟着变了。之后的权限提示
   不再自动放行是**预期行为**。`api: keys …` / `api: send …` 同理：那是配套程序
   打的字，不是本工具自己发的。这两行只记**动作**不记内容 ——
   `lines=1`、`<24 chars>{Enter}`（2.1.2 起；键名保留，打的字只剩字数）。
   想知道对方到底打了什么，去问那个程序，日志里没有。

## 排查：某个会话没被接上

按顺序问日志，别跳步：

1. **这个窗口在覆盖范围里吗** —— 日志里有没有出现过它的 id？
   没有就是没被看见：检查是不是被 **Exclude** 掉了，或者那个 Claude Code
   开在了 Windows Terminal 的**标签页**里——只有**当前活动标签**会被读到，
   每个会话拖成独立窗口才都能被看住。
2. **当时读得到屏幕吗** —— 附近有没有 `could not read the screen of #id`。
   偶发一两轮是 UIA 打嗝，会自己好（好了会写 `screen of #id readable again`）。
   另一种是整个窗口这一轮没枚举到：`N open window(s) missing from this pass (#id …)`
   —— 状态会保留，不会重来；2.1.2 起括号里写明是哪几个窗口，
   **同一个 id 反复出现**才值得查，零星几次是正常的。
   **一直不好、再也没恢复**，多半是这个窗口的会话进程已经被杀掉了，
   而终端窗口壳子还留着（典型：某个脚本 `taskkill /PID <claude> /T /F` 之后没关窗口）。
   这种窗口 Win32 看还在、UIA 也枚举得到，但 ConPTY 已死，文本读取永远 E_FAIL ——
   **它不会自己恢复，关掉这个窗口即可**。在它关掉之前，这个"窗口"一直占着一格覆盖数，
   而里面根本没有会话。
3. **它是不是判断成「不该动」** —— 看有没有上面「故意不管」的那几条。
4. **发了但没送达吗** —— `retry send failed for …; will try again`
   说明按键没送出去（通常是发送瞬间焦点被别的窗口抢走），下一轮会自己重来。
5. **模型额度用完（`You've reached your Fable limit …`）却没切模型** ——
   「额度用完就切模型」只对**模型恢复作用范围内**的窗口生效：Advanced… → 模型恢复
   里勾选的窗口，或勾 **All windows**。开关都开着但一个窗口都没选，它就永远不动。
   2.1.2 起日志会直说：`… is out of quota on its model, but model recovery is not set
   up for this window — not switching`；开着却没选窗口时会写
   `model recovery is ON but applies to no window`。
6. **是不是根本没到时间** —— `will fire at …` 那行写着预定时刻，
   实际触发会晚一个轮询周期以内（默认 60 秒），这是正常的。

## 常用设置（Advanced…）

- **Poll interval**（默认 60 秒）读每个窗口的间隔；**Buffer**（默认 60 秒）
  重置时间之后多等一会儿再发，因为限制常常比写的时间晚一点点解除。
- **Retry interval**（默认 600 秒）网络卡住时两次 `continue` 的间隔。调太短只会刷屏。
- **Triggers** —— 四类横幅的识别正则可以自己改。Anthropic 改文案时这里先失效，
  也是第一个该看的地方。
- **模型恢复** / **跑完接下一棒（after-finish）** —— 都是**默认关闭、按窗口开启**；
  会自动打字的功能带次数上限，默认只跑一次，用完要手动再开。

## 报问题给作者

`Export log for feedback…`（2.0.18 起，主界面按钮）导出一份**脱敏**日志：
窗口标题和用户自己输入的提示词全部抹掉，窗口只以 `#id` 出现，
斜杠命令和 `continue` 保留（它们不描述任何工作内容）。
**写盘前会把全文摊出来让人过目**，确认了才保存。

原始日志留在本机没问题，但直接交出去等于列出这台机器在做的项目——
Claude Code 的窗口是按对话内容命名的。所以要发的话用这个导出，别发原文件。

发到哪：下载站 **http://106.14.76.130** 右下角的咨询窗口，可以带文字和截图。

## 授权

MIT 开源，免费，可商用。源码与发布：
`https://github.com/zhh198903-ctrl/claude-code-auto-continue`
