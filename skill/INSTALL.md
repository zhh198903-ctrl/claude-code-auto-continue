# 安装这个 skill

把 `auto-continue` 这个文件夹整个放进 Claude Code 的技能目录：

（如果你下的是 Auto-Continue 主程序包，里面那个文件夹叫 `skill`，
复制过去时改名成 `auto-continue` 即可。）


```
%USERPROFILE%\.claude\skills\auto-continue\SKILL.md
```

也就是说复制完长这样：

```
C:\Users\<你>\.claude\skills\auto-continue\
    SKILL.md
    INSTALL.md
```

新开一个 Claude Code 会话即可生效。装好之后可以直接问它：

- 「auto-continue 为什么没接上我那个会话？」
- 「帮我看下 activity.log 最近有没有异常」
- 「我要把日志发给作者，怎么脱敏导出？」
- 「模型恢复这个功能怎么配？」

它会去读本机的活动日志再回答，而不是凭印象猜。

装不装都不影响 Auto-Continue 本身运行——这只是让 Claude Code 懂得怎么帮你
看它的日志、怎么配置它。

---

# Installing this skill

Copy the `skill` folder to `%USERPROFILE%\.claude\skills\auto-continue\`,
then start a new Claude Code session. It teaches Claude Code to read
Auto-Continue's activity log and help you configure it. Auto-Continue runs
fine without it.

MIT licensed, free, commercial use permitted.
