# 多会话并排切分（y 轴 split）

把中间会话区竖直切分成左右并排的几列，每列跑一个不同的 live Claude session，
同时看多个、随时切焦点打字。每列顶上有彩色条，按 session id 稳定着色，一眼区分。

```
┌─board─┬─claude·a1b2(主)─┬─claude·c3d4────┬─claude·e5f6────┐
│ 卡片  │ > _             │ asst: 跑测试中 │ user: 继续吧   │
│ ●○●○  │ asst: ...        │ (live, 只读看) │ (live)         │
└───────┴──────────────────┴────────────────┴────────────────┘
        └── 中间会话区,y 轴切分(竖直分割线),各 pane 不同色 ──┘
```

## 用法（鼠标为主）

看板每张卡片最左边有一个圆点：

- `○`（空心）= 没钉 → **点它** = 钉进当前窗口中间并排
- `●`（彩色实心）= 已钉（颜色跟该 pane 一致）→ **再点** = 取消（关掉那列，session 留盘）
- 点中间某个 pane = 聚焦它（tmux 鼠标已开，直接点）

钉的动作只有这一个：**点圆点**。不用记快捷键。

### 钉的时候会发生什么（自动判断）

| 该 session 当前状态 | 点圆点的效果 |
|---|---|
| 没开过（done / ext 已退出） | 中间切一列 + `claude --resume` 开起来 |
| 在**别的窗口**开着 | 那边杀掉（进程一并 SIGKILL）+ 这边重新 resume |
| 已在**当前窗口** | 只聚焦那列，不重复开 |

> 一个 sid 全局只有一个 live 实例，不会双开。
> 搬运"在别窗开着"的 session 用的是「关旧 + resume 新」，不是 move-pane，所以不会丢窗口、不留孤儿进程。
> 代价：搬一个正在**忙**的 session 会打断它一下（kill+resume）；idle/waiting 的随便搬，无感。

### 键盘别名（可选）

- 看板里 `v` = 钉选中卡片（等同点空心圆点）
- 看板里 `V` = 取消选中卡片
- `Ctrl-b V` = 杀掉当前聚焦的那列会话 pane
- `Ctrl-b B` = 多列时循环切焦点到下一列

## 颜色

每个 session 的颜色由 `sid8` 哈希到 8 色调色板（tmux 256 色），稳定不变：
`a1b2…` 永远是那个色。颜色画在 pane 顶部的边框条上（`pane-border-status top`），
显示 `cw·<sid8>`。board/services pane 顶上也有标题条（无色）。

## 怎么追踪哪个 pane 是哪个 session

Claude 运行时会把 tmux pane 标题改写成 `✳ <当前任务>`，把 `cw·claude·<sid8>` 冲掉。
所以 sid **不存标题**，而是存在 pane 的用户选项 `@cwsid`（`tmux set-option -p` 设的，
claude 改不了）里。颜色存 `@cwtint`。`cw up` 时 `_migrate_claude_panes` 会给老 pane
补上 `@cwsid`（按窗口名里的 sid8 定位那个非 board/非 services 的 pane）。

## CLI

```
cw pin <sid>              把 session 钉到当前窗口中间并排
cw unpin <sid8|current>   取消钉选（sid8 或 current=当前聚焦的 pane）
```

`cw up` 会 respawn 看板/服务 pane，让新逻辑（圆点、新分支）立即生效。

## 排查

- **点圆点 / 按 v 只闪一下，没出 pane**：`claude --resume` 秒退。最常见原因是
  `claude` 不在 PATH 上（近期重装可能只留 `claude.exe`、没建软链）。`cw` 已内置
  `_claude_bin()` 自动找 `claude.exe` 的绝对路径；如果还是不行，跑
  `python3 -c "import cw; print(cw._claude_bin())"` 看解析到什么，确认可执行。
- **圆点不显示 / 点了没反应**：看板 pane 可能是旧代码，重跑 `cw up`（会 respawn 看板）。
  确认 `tmux show-option -t cw mouse` 是 `on`。
- **钉的是当前窗口自己的会话，没切分**：这是对的——它已经在这了，没法跟自己并排。
  选「另一个」会话点圆点才会切分。
- **某 session 报"已在 cw 的某个窗格里运行但未被追踪"**：它在某个窗口名没 sid8 的
  pane 里裸跑（如 `main` 窗）。先 `cw up` 追踪，或手动关掉它再钉。
- **切太多列很窄**：中间区只有 ~55% 宽，≥4 列每列就 <24 字符。建议 ≤3 列，或用
  `Ctrl-b z` 临时放大某一列。
- **取消钉选后 claude 进程没死（孤儿）**：新版 unpin 会 SIGKILL 该 sid 的 pid；老版
  留下的孤儿可用 `cw unpin <sid8>` 或直接 `kill <pid>` 清掉。

## 设计要点

- **纯标准库 + tmux 原生**，无新依赖。颜色/标签靠 tmux pane 选项和 `pane-border-format`。
- **一个 sid 一个 live 实例**：pin 前查 `managed_panes`（`@cwsid`）和 `_sid_in_cw_tmux`
  （pid 的 tty 是否在 cw pane 里），绝不二次 resume。裸终端里的 ext session 放行（等同 import）。
- **复用空 shell**：当前窗口若有个没跑 claude 的空 pane（标题恰为 `cw·claude`），第一个 pin
  会 `respawn-pane` 复用它，而不是在旁边再切一个死 shell。
- **等宽重排**：`_balance_claude_panes` 只调会话 pane 的宽度，不动 board/services 比例。
