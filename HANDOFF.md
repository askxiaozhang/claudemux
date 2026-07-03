# 交接说明:claudemux 截图核查

> 给能读图片的 agent。当前主模型只支持文本,无法目视截图,所以把这步交给你。

## 背景

`claudemux`(命令 `cw`)是一个 tmux + curses 的 Claude 多会话看板 TUI。
仓库:https://github.com/askxiaozhang/claudemux
本地路径:`/Users/zhangchang/gitlab/claude-wekan`

README 顶部用一张截图当主图,需要你核查这张图的**视觉质量**是否达标。

## 需要你做的事

### 1. 看这张图

`/Users/zhangchang/gitlab/claude-wekan/docs/board.png`(1300×674,PNG)

它是**真实**的 `cw board --demo` 终端输出捕获后转成的 PNG(不是手绘)。
应该长这样(文本布局参考):

```
claudemux │ RUNNING:1 WAITING:2 EXTERNAL:3 DONE:2 │ ↑↓move ⏎switch/reply n:new i:import r:refresh q:quit

── RUNNING (1) ──
▸ ● [web-app]     refactor auth flow      ☑1/3  refactor the auth flow …  3m
── WAITING (needs you) (2) ──
  ? [api-server]  deploy to staging             deploy api-server …       2m
  ? [docs-site]   add API reference             add API reference docs    5m
── EXTERNAL (importable) (3) ──
  ● [cli-tool]    add --verbose                 add a --verbose flag      30s
  ○ [mobile-app]  fix push notif                fix push notifications…   12m
  ○ [ml-pipeline] retrain model                 retrain the ranking …     1h
── DONE (2) ──
  ✓ [web-app]     write unit tests              write unit tests for auth 1h
  ✗ [scripts]     cleanup backups               cleanup old backups       3h
────────────────────────────────────────────────────────────
task   refactor the auth flow — extract AuthProvider and add tests
todos  ☑ Refactor AuthProvider · ☑ Add unit tests · ☐ Update docs
cwd    ~/projects/web-app     branch  feat/auth-refactor
```

### 2. 核查清单(逐项确认)

- [ ] 四个分组标题颜色区分明显:RUNNING 绿、WAITING 黄、EXTERNAL 蓝、DONE 青
- [ ] 选中行(web-app 那行,行首 `▸`)有高亮反色条,清晰可读
- [ ] 状态符号正确:`●`=忙/运行,`○`=空闲,`?`=等待输入,`✓`=完成,`✗`=失败
- [ ] 文字**没有被截断/重叠**,右侧的时间(3m/2m/…)完整可见
- [ ] 底部详情区(task/todos/cwd/branch)完整显示,没被裁掉
- [ ] 终端窗口外观正常(标题栏三个红黄绿圆点、圆角、深色背景)
- [ ] 整体没有多余空白或图像被裁得过紧/过松

### 3. 如果有问题

生成这张 PNG 的脚本在 `/tmp/shoot.py`(纯 Python + tmux + headless Chrome,
无第三方依赖)。它的流程:
1. `tmux new-session -d -s cwshot -x 150 -y 30`
2. 在里面跑 `python3 cw.py board --demo`,等 1.6s
3. `tmux capture-pane -p -e` 抓带 ANSI 颜色的屏幕
4. 把 ANSI 转成 HTML(带终端窗口 chrome 样式)写到 `docs/_shot.html`
5. headless Chrome 截图成 `docs/board.png`

改进方向若需要:
- 图太紧/太松 → 调 `shoot.py` 里 `--window-size=1300,%d` 的宽度或 `win_h` 公式
- 想换配色 → 改 `COLORS` 字典或 `_shot.html` 的 CSS
- demo 数据不合适 → 改 `cw.py` 里的 `demo_cards()` 函数
- 重新生成:`python3 /tmp/shoot.py`(会覆盖 docs/board.png)

### 4. 备用图

`docs/board.svg` 是一张**手绘矢量版**(非真实截图),布局相同。
如果 PNG 实在有问题且短期修不好,可以把 README 第 7 行改回:
`![claudemux board](docs/board.svg)`

## 当前状态

- README.md 第 7 行已指向 `docs/board.png`
- 已提交的还是旧版(引用 svg);**docs/board.png 和 README 改动尚未提交**
- 待你核查通过后,可以提交推送:
  ```
  cd /Users/zhangchang/gitlab/claude-wekan
  git add -A && git commit -m "docs: use real terminal screenshot for board"
  git push
  ```
