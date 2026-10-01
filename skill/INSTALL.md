# 安装这个 skill

把 `auto-continue` 文件夹放进所用 CLI 的技能目录：

（如果你下的是 Auto-Continue 主程序包，里面那个文件夹叫 `skill`，
复制过去时改名成 `auto-continue` 即可。）


```
Claude Code: %USERPROFILE%\.claude\skills\auto-continue\SKILL.md
Codex CLI:   %USERPROFILE%\.agents\skills\auto-continue\SKILL.md
```

也就是说复制完长这样：

```
C:\Users\<你>\.claude\skills\auto-continue\
    SKILL.md
    INSTALL.md
```

使用 Codex 时，把下方示例中的 `.claude` 换成 `.agents`。两个 CLI 都用时，分别复制到两个目录。新开对应 CLI 会话即可生效。装好之后可以直接问它：

- 「auto-continue 为什么没接上我那个会话？」
- 「帮我看下 activity.log 最近有没有异常」
- 「我要把日志发给作者，怎么脱敏导出？」
- 「模型恢复这个功能怎么配？」

它会去读本机的活动日志再回答，而不是凭印象猜。

装不装都不影响 Auto-Continue 本身运行——这只是让 CLI 懂得怎么帮你
看它的日志、怎么配置它。

---

# Installing this skill

Copy the `skill` folder to `%USERPROFILE%\.claude\skills\auto-continue\`
for Claude Code, or `%USERPROFILE%\.agents\skills\auto-continue\` for Codex CLI.
Copy to both directories if using both CLIs, then start a new session. It teaches the CLI to read
Auto-Continue's activity log and help you configure it. Auto-Continue runs
fine without it.

MIT licensed, free, commercial use permitted.
