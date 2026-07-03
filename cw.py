#!/usr/bin/env python3
"""cw — Claude 多终端调度器 (claude-wekan).

tmux 当终端复用骨架,curses 看板做调度层。纯 Python 标准库,无 pip/npm。

用法:
  cw up              起/连 tmux 会话 cw,绑定 Ctrl-b b 召出看板
  cw board           运行看板 TUI(popup 或普通终端里都可)
  cw launch <cwd>    在 cw 会话新建窗口跑 claude(可选初始 prompt)
  cw import <sid>    用 claude --resume <sid> 把现有会话接进 tmux
  cw status          打印发现的会话/作业(JSON,调试用)
  cw list            打印看板卡片(纯文本)
"""
import os, sys, json, glob, subprocess, time, re, shlex, curses, argparse

SESSION_NAME = "cw"
SID8_RE = re.compile(r"-(?P<sid>[0-9a-f]{8})$")
SCRIPT = os.path.realpath(__file__)


# --------------------------------------------------------------------------
# 数据源
# --------------------------------------------------------------------------

def expand(p):
    return os.path.expanduser(p)


def default_sources():
    out = []
    for p in ("~/.claude-doubao", "~/.claude"):
        b = expand(p)
        if os.path.isdir(b):
            out.append(b)
    return out


def list_sessions(base):
    res = []
    for f in glob.glob(os.path.join(base, "sessions", "*.json")):
        try:
            o = json.load(open(f))
        except Exception:
            continue
        pid = o.get("pid")
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        o["alive"] = alive
        o["source"] = base
        res.append(o)
    return res


def list_jobs(base):
    res = []
    for d in glob.glob(os.path.join(base, "jobs", "*")):
        if not os.path.isdir(d):
            continue
        st = os.path.join(d, "state.json")
        if not os.path.isfile(st):
            continue
        try:
            o = json.load(open(st))
        except Exception:
            continue
        o["jobdir"] = d
        o["source"] = base
        res.append(o)
    return res


def find_transcript(base, session_id):
    g = glob.glob(os.path.join(base, "projects", "*", session_id + ".jsonl"))
    return g[0] if g else None


def tail_lines(path, n=80):
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    chunk = 1 << 16
    data = b""
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos = size
        while pos > 0 and data.count(b"\n") <= n:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    return data.decode("utf-8", "replace").splitlines()[-n:]


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b["text"] for b in content
                 if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        return "\n".join(parts) if parts else None
    return None


def parse_transcript(path, n=80):
    info = {"title": None, "last_prompt": None, "last_user": None,
            "last_asst": None, "todos": [], "gitBranch": None,
            "cwd": None, "last_ts": None}
    if not path or not os.path.isfile(path):
        return info
    for line in tail_lines(path, n):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        t = o.get("type")
        if t == "ai-title":
            info["title"] = o.get("aiTitle")
        elif t == "last-prompt":
            info["last_prompt"] = o.get("lastPrompt")
        elif t in ("user", "assistant"):
            m = o.get("message") or {}
            c = m.get("content")
            txt = _text(c)
            if txt:
                info["last_user" if t == "user" else "last_asst"] = txt
            if o.get("gitBranch"):
                info["gitBranch"] = o.get("gitBranch")
            if o.get("cwd"):
                info["cwd"] = o.get("cwd")
            if o.get("timestamp"):
                info["last_ts"] = o.get("timestamp")
            if t == "assistant" and isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_use" \
                            and b.get("name") == "TodoWrite":
                        td = (b.get("input") or {}).get("todos")
                        if isinstance(td, list):
                            info["todos"] = td
    return info


# --------------------------------------------------------------------------
# tmux
# --------------------------------------------------------------------------

def tmux(args, capture=True):
    try:
        r = subprocess.run(["tmux"] + args, capture_output=capture, text=True)
        if capture:
            return r.returncode, r.stdout, r.stderr
        return r.returncode, "", ""
    except FileNotFoundError:
        return -1, "", "tmux not found"


def tmux_windows(session=SESSION_NAME):
    rc, out, _ = tmux(["list-windows", "-t", session, "-F",
                       "#{window_index}\t#{window_name}\t#{window_active}"])
    if rc != 0:
        return []
    wins = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            wins.append({"index": parts[0], "name": parts[1],
                         "active": parts[2] == "1"})
    return wins


def managed_sids(session=SESSION_NAME):
    """sid8 -> window name, for Claude windows we launched."""
    m = {}
    for w in tmux_windows(session):
        mt = SID8_RE.search(w["name"])
        if mt:
            m[mt.group("sid")] = w["name"]
    return m


def projshort(cwd):
    if not cwd:
        return "?"
    return os.path.basename(cwd.rstrip("/")) or cwd


# --------------------------------------------------------------------------
# 卡片聚合
# --------------------------------------------------------------------------

def age_str(ts_ms):
    if not ts_ms:
        return ""
    dt = (time.time() * 1000 - ts_ms) / 1000
    if dt < 60:
        return "%ds" % int(dt)
    if dt < 3600:
        return "%dm" % int(dt / 60)
    if dt < 86400:
        return "%dh" % int(dt / 3600)
    return "%dd" % int(dt / 86400)


def todo_summary(todos):
    if not todos:
        return ""
    done = sum(1 for t in todos if (t.get("status") or "").lower() in ("completed", "done"))
    return "%d/%d" % (done, len(todos))


def _dedup_by_sid(items, key="sessionId"):
    seen, out = set(), []
    for it in items:
        sid = it.get(key)
        if sid and sid in seen:
            continue
        if sid:
            seen.add(sid)
        out.append(it)
    return out


def gather_cards():
    sources = default_sources()
    sessions, jobs = [], []
    for b in sources:
        sessions += list_sessions(b)
        jobs += list_jobs(b)
    # .claude 与 .claude-doubao 会互相镜像,按 sessionId 去重(保留 doubao 在前)
    sessions = _dedup_by_sid(sessions)
    jobs = _dedup_by_sid(jobs)
    managed = managed_sids()

    cards = []
    for s in sessions:
        sid = s.get("sessionId", "") or ""
        sid8 = sid[:8]
        base = s.get("source")
        tr = parse_transcript(find_transcript(base, sid))
        cwd = s.get("cwd") or tr.get("cwd")
        is_managed = sid8 in managed
        status = s.get("status")
        if not s.get("alive"):
            group = "done"
        elif is_managed:
            group = "running" if status == "busy" else "waiting"
        else:
            group = "external"
        cards.append({
            "kind": "interactive", "sid": sid, "sid8": sid8, "cwd": cwd,
            "name": s.get("name") or tr["title"] or "(unnamed)",
            "title": tr["title"], "status": status, "alive": s.get("alive"),
            "group": group, "startedAt": s.get("startedAt"),
            "updatedAt": s.get("updatedAt"), "managed": is_managed,
            "win": managed.get(sid8), "tr": tr, "needs": None,
        })
    for j in jobs:
        st = (j.get("state") or "").lower()
        lsp = j.get("linkScanPath")
        tr = parse_transcript(lsp) if lsp else parse_transcript(None)
        sid = j.get("sessionId") or os.path.basename(j.get("jobdir", ""))
        cwd = j.get("cwd") or tr.get("cwd")
        needs = j.get("needs") or j.get("detail")
        if st in ("completed", "failed", "done", "canceled", "cancelled"):
            group = "done"
        elif st == "blocked" or st in ("waiting", "needs-input", "paused") or needs:
            group = "waiting"
        else:
            group = "running"
        cards.append({
            "kind": "bg", "sid": sid, "sid8": sid[:8], "cwd": cwd,
            "name": j.get("name") or tr["title"] or "bg", "title": tr["title"],
            "status": st, "alive": st in ("running", "blocked"), "group": group,
            "needs": needs, "managed": False,
            "win": None, "tr": tr, "tokens": j.get("tokens"),
        })
    return cards


GROUPS = [
    ("running", "RUNNING", 2),
    ("waiting", "WAITING (needs you)", 3),
    ("external", "EXTERNAL (importable)", 4),
    ("done", "DONE", 5),
]


# --------------------------------------------------------------------------
# 编排:launch / import / up
# --------------------------------------------------------------------------

def resolve_new_sid(cwd, timeout=12):
    cwd = os.path.abspath(cwd)
    initial = set()
    for b in default_sources():
        for s in list_sessions(b):
            initial.add(s.get("sessionId"))
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        for b in default_sources():
            for s in list_sessions(b):
                if s.get("sessionId") in initial:
                    continue
                if os.path.abspath(s.get("cwd") or "") == cwd:
                    return s.get("sessionId")
    return None


def find_session(sid):
    """返回 (base, cwd) for an interactive session id."""
    for b in default_sources():
        for s in list_sessions(b):
            if s.get("sessionId") == sid:
                return b, s.get("cwd")
        # 也可能在转录里(已退出的)
        for d in glob.glob(os.path.join(b, "projects", "*")):
            p = os.path.join(d, sid + ".jsonl")
            if os.path.isfile(p):
                tr = parse_transcript(p)
                if tr.get("cwd"):
                    return b, tr["cwd"]
    return None, None


def cmd_launch(cwd, prompt=None):
    cwd = os.path.abspath(expand(cwd))
    if not os.path.isdir(cwd):
        print("cwd 不存在: %s" % cwd, file=sys.stderr)
        return 1
    name = projshort(cwd)[:18]
    shell_cmd = "claude"
    if prompt:
        shell_cmd = "claude " + shlex.quote(prompt)
    rc, _, err = tmux(["new-window", "-t", SESSION_NAME, "-n", name,
                       "-c", cwd, shell_cmd])
    if rc != 0:
        print("tmux new-window 失败(先 `cw up`?): %s" % err, file=sys.stderr)
        return 1
    print("启动 claude @ %s,等待 sessionId..." % cwd)
    sid = resolve_new_sid(cwd, timeout=12)
    if sid:
        nm = "%s-%s" % (name, sid[:8])
        tmux(["rename-window", "-t", "%s:%s" % (SESSION_NAME, name), nm])
        print("已建窗口 %s (sid %s)" % (nm, sid))
    else:
        print("未能在 12s 内解析 sessionId(窗口已建,看板仍可工作)")
    return 0


def cmd_import(sid):
    base, cwd = find_session(sid)
    if not cwd:
        print("找不到 session %s 的 cwd" % sid, file=sys.stderr)
        return 1
    name = "%s-%s" % (projshort(cwd)[:18], sid[:8])
    rc, _, err = tmux(["new-window", "-t", SESSION_NAME, "-n", name,
                       "-c", cwd, "claude --resume " + shlex.quote(sid)])
    if rc != 0:
        print("tmux new-window 失败: %s" % err, file=sys.stderr)
        return 1
    print("已导入 %s @ %s(记得关掉旧裸终端以防冲突)" % (name, cwd))
    return 0


def cmd_up():
    rc, _, _ = tmux(["has-session", "-t", SESSION_NAME])
    if rc != 0:
        tmux(["new-session", "-d", "-s", SESSION_NAME])
        print("已创建 tmux 会话 %s" % SESSION_NAME)
    # 绑定 Ctrl-b b 召出看板(bind-key 不接受 -t;默认绑到 prefix 表,全局生效)
    rc, _, err = tmux(["bind-key", "b", "display-popup", "-E",
                       "-w", "80%", "-h", "80%", "python3 %s board" % SCRIPT])
    if rc != 0:
        print("绑定 Ctrl-b b 失败: %s" % err, file=sys.stderr)
    # 绑定 Ctrl-b N 直接新建
    rc, _, err = tmux(["bind-key", "N", "command-prompt",
                       "-p", "cwd:", "run-shell 'python3 %s launch \"%%1\"'" % SCRIPT])
    if rc != 0:
        print("绑定 Ctrl-b N 失败: %s" % err, file=sys.stderr)
    print("Ctrl-b b = 看板   Ctrl-b N = 新建 Claude   (会话: %s)" % SESSION_NAME)
    if os.environ.get("TMUX"):
        tmux(["switch-client", "-t", SESSION_NAME])
    else:
        os.execvp("tmux", ["tmux", "attach", "-t", SESSION_NAME])
    return 0


# --------------------------------------------------------------------------
# 看板 TUI
# --------------------------------------------------------------------------

def _init_colors():
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except Exception:
        bg = curses.COLOR_BLACK
    curses.init_pair(2, curses.COLOR_GREEN, bg)
    curses.init_pair(3, curses.COLOR_YELLOW, bg)
    curses.init_pair(4, curses.COLOR_BLUE, bg)
    curses.init_pair(5, curses.COLOR_CYAN, bg)
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)


def _truncate(s, n):
    if not s:
        return ""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:max(0, n - 1)] + "…"


def _build_flat(state):
    flat = []
    by_group = {g: [] for g, _, _ in GROUPS}
    for c in state["cards"]:
        by_group.setdefault(c["group"], []).append(c)
    for g, _, _ in GROUPS:
        for c in sorted(by_group.get(g, []),
                        key=lambda c: -(c.get("updatedAt") or c.get("startedAt") or 0)):
            flat.append(c)
    state["flat"] = flat
    if state["sel"] >= len(flat):
        state["sel"] = max(0, len(flat) - 1)


def _draw(stdscr, state):
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    # header
    counts = {g: 0 for g, _, _ in GROUPS}
    for c in state["cards"]:
        counts[c["group"]] = counts.get(c["group"], 0) + 1
    head = "claude-wekan  │  " + "  ".join("%s:%d" % (g.upper(), counts.get(g, 0))
                                            for g, _, _ in GROUPS)
    head += "  │  ↑↓move ⏎switch n:new i:import r:refresh q:quit"
    stdscr.addnstr(0, 0, _truncate(head, w), w, curses.A_BOLD)
    row = 2
    sel = state["flat"][state["sel"]] if state["flat"] else None
    for gkey, gname, gcol in GROUPS:
        items = [c for c in state["flat"] if c["group"] == gkey]
        if not items:
            continue
        if row >= h - 1:
            break
        stdscr.addnstr(row, 0, "── %s (%d) ──" % (gname, len(items)), w,
                       curses.color_pair(gcol) | curses.A_BOLD)
        row += 1
        for c in items:
            if row >= h - 6:
                break
            mark = "▸" if c is sel else " "
            stat = {"busy": "●", "idle": "○", "running": "●",
                    "blocked": "?", "completed": "✓", "failed": "✗"}.get(c.get("status"), "·")
            proj = "[%s]" % _truncate(projshort(c.get("cwd")), 16)
            title = _truncate(c.get("title") or c.get("name"), 24)
            task = _truncate(c.get("tr", {}).get("last_prompt") or c.get("tr", {}).get("last_user"), 30)
            td = todo_summary(c.get("tr", {}).get("todos"))
            td = ("☑%s " % td) if td else ""
            ag = age_str(c.get("updatedAt") or c.get("startedAt"))
            line = "%s %s %s %-26s %s%s%s" % (mark, stat, proj, title, td, _truncate(task, 28), ag)
            attr = curses.color_pair(gcol)
            if c is sel:
                attr = curses.color_pair(6) | curses.A_BOLD
            stdscr.addnstr(row, 0, _truncate(line, w), w, attr)
            row += 1
        row += 1
    # footer: selected detail
    if sel and row < h:
        stdscr.addnstr(min(row, h - 6), 0, "─" * w, w, curses.A_DIM)
        _draw_detail(stdscr, sel, min(row, h - 6) + 1, w, h)


def _draw_detail(stdscr, c, top, w, h):
    tr = c.get("tr", {}) or {}
    lines = []
    lines.append(("name", c.get("name")))
    lines.append(("cwd", c.get("cwd")))
    lines.append(("task", tr.get("last_prompt") or tr.get("last_user")))
    asst = tr.get("last_asst")
    if asst:
        lines.append(("last", asst))
    if c.get("needs"):
        lines.append(("needs", c.get("needs")))
    todos = tr.get("todos")
    if todos:
        lines.append(("todos", " | ".join("%s[%s]" % (_truncate(t.get("content"), 30),
                                                       (t.get("status") or "?")[0].upper())
                                          for t in todos)))
    gb = tr.get("gitBranch")
    if gb:
        lines.append(("branch", gb))
    r = top
    for k, v in lines:
        if r >= h:
            break
        if v:
            stdscr.addnstr(r, 0, _truncate("%-6s %s" % (k, v), w), w,
                           curses.color_pair(5) if k in ("needs", "task") else curses.A_NORMAL)
            r += 1


def _edit_line(stdscr, y, x, prompt, default, width):
    """极简单行编辑器。返回字符串或 None(Esc)。"""
    curses.curs_set(1)
    buf = list(default or "")
    while True:
        stdscr.addnstr(y, x, prompt + "".join(buf) + " ", width, curses.A_REVERSE)
        stdscr.clrtoeol()
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            curses.curs_set(0)
            return "".join(buf)
        if ch in (27, ord("q")) and not buf:
            curses.curs_set(0)
            return None
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if buf:
                buf.pop()
        elif 32 <= ch < 256:
            buf.append(chr(ch))
        elif ch == curses.KEY_DOWN or ch == 9:
            curses.curs_set(0)
            return "".join(buf) or None


def _activate(state, stdscr):
    """处理选中卡片的动作。返回 True 表示应退出看板(已切窗口)。"""
    if not state["flat"]:
        return False
    c = state["flat"][state["sel"]]
    if c["managed"] and c["win"]:
        tmux(["select-window", "-t", "%s:%s" % (SESSION_NAME, c["win"])])
        return True
    if c["group"] == "external" and c.get("sid"):
        # 导入并切换
        name = "%s-%s" % (projshort(c.get("cwd"))[:18], c["sid8"])
        cwd = c.get("cwd") or "."
        rc, _, err = tmux(["new-window", "-t", SESSION_NAME, "-n", name, "-c", cwd,
                           "claude --resume " + shlex.quote(c["sid"])])
        if rc == 0:
            tmux(["select-window", "-t", "%s:%s" % (SESSION_NAME, name)])
            return True
        state["msg"] = "导入失败: %s" % err
        return False
    if c["kind"] == "bg" and c.get("needs"):
        # 后台 agent 需要输入:MVP 仅提示,不内嵌回复
        state["msg"] = "该后台 agent 需要输入(回复功能在快进项): %s" % _truncate(c["needs"], 60)
        return False
    return False


def _new_session(stdscr, state):
    h, w = stdscr.getmaxyx()
    known = []
    seen = set()
    for c in state["cards"]:
        cw = c.get("cwd")
        if cw and cw not in seen:
            seen.add(cw)
            known.append(cw)
    # .claude.json projects
    for b in default_sources():
        try:
            cfg = json.load(open(os.path.join(b, ".claude.json")))
            for p in (cfg.get("projects") or {}):
                if p not in seen:
                    seen.add(p)
                    known.append(p)
        except Exception:
            pass
    default_cwd = known[0] if known else os.getcwd()
    stdscr.erase()
    stdscr.addnstr(0, 0, "新建 Claude 会话(Enter 确认 / Esc 取消)", w, curses.A_BOLD)
    for i, p in enumerate(known[:10]):
        stdscr.addnstr(2 + i, 2, "%d. %s" % (i + 1, _truncate(p, w - 6)), w, curses.A_DIM)
    cwd = _edit_line(stdscr, h - 4, 0, "cwd: ", default_cwd, w)
    if not cwd:
        return
    prompt = _edit_line(stdscr, h - 2, 0, "prompt(可空): ", "", w)
    cwd = os.path.abspath(expand(cwd))
    name = projshort(cwd)[:18]
    shell_cmd = "claude"
    if prompt:
        shell_cmd = "claude " + shlex.quote(prompt)
    rc, _, err = tmux(["new-window", "-t", SESSION_NAME, "-n", name, "-c", cwd, shell_cmd])
    if rc != 0:
        state["msg"] = "新建失败: %s" % err
        return
    sid = resolve_new_sid(cwd, timeout=12)
    if sid:
        nm = "%s-%s" % (name, sid[:8])
        tmux(["rename-window", "-t", "%s:%s" % (SESSION_NAME, name), nm])
    state["msg"] = "已新建 @ %s" % cwd
    state["cards"] = gather_cards()


def _board_main(stdscr):
    _init_colors()
    curses.curs_set(0)
    curses.halfdelay(30)  # 3s 超时 -> 自动刷新
    state = {"cards": gather_cards(), "sel": 0, "flat": [], "msg": ""}
    while True:
        _build_flat(state)
        try:
            _draw(stdscr, state)
            if state.get("msg"):
                h, w = stdscr.getmaxyx()
                stdscr.addnstr(h - 1, 0, _truncate(state["msg"], w), w, curses.color_pair(3))
        except curses.error:
            pass  # 窗口过小时忽略绘制错误,不崩溃
        ch = stdscr.getch()
        if ch == -1:
            state["cards"] = gather_cards()
            continue
        if ch in (ord("q"),):
            break
        elif ch == ord("r"):
            state["cards"] = gather_cards()
        elif ch in (curses.KEY_DOWN, ord("j")):
            state["sel"] = min(state["sel"] + 1, max(0, len(state["flat"]) - 1))
        elif ch in (curses.KEY_UP, ord("k")):
            state["sel"] = max(state["sel"] - 1, 0)
        elif ch in (10, 13, curses.KEY_ENTER):
            if _activate(state, stdscr):
                break
        elif ch == ord("n"):
            _new_session(stdscr, state)
        elif ch == ord("i"):
            if state["flat"] and state["flat"][state["sel"]].get("group") == "external":
                if _activate(state, stdscr):
                    break
            else:
                state["msg"] = "选中一个 EXTERNAL 卡片再用 i 导入"


def cmd_board():
    try:
        curses.wrapper(_board_main)
    except curses.error as e:
        print("curses 错误(终端太小?): %s" % e, file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
# 文本输出:status / list
# --------------------------------------------------------------------------

def cmd_status():
    print(json.dumps(gather_cards(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_list():
    cards = gather_cards()
    if not cards:
        print("(没发现任何 Claude 会话)")
        return 0
    for gkey, gname, _ in GROUPS:
        items = [c for c in cards if c["group"] == gkey]
        if not items:
            continue
        print("\n── %s (%d) ──" % (gname, len(items)))
        for c in sorted(items, key=lambda c: -(c.get("updatedAt") or c.get("startedAt") or 0)):
            tr = c.get("tr", {}) or {}
            stat = c.get("status") or "?"
            proj = projshort(c.get("cwd"))
            title = _truncate(tr.get("title") or c.get("name"), 30)
            task = _truncate(tr.get("last_prompt") or tr.get("last_user"), 50)
            td = todo_summary(tr.get("todos"))
            ag = age_str(c.get("updatedAt") or c.get("startedAt"))
            if c.get("managed"):
                mkind = "tmux"
            elif c.get("kind") == "bg":
                mkind = "bg"
            else:
                mkind = "ext"
            print("  [%-7s] %-16s %-30s %-4s  %s%s" % (stat, proj, title, mkind,
                                                     ("☑%s " % td if td else ""), ag))
            if task:
                print("             task: %s" % task)
            if c.get("needs"):
                print("             needs: %s" % _truncate(c["needs"], 70))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(prog="cw", description="Claude 多终端调度器")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("up", help="起/连 tmux 会话并绑定看板热键")
    sub.add_parser("board", help="运行看板 TUI")
    sub.add_parser("status", help="打印会话/作业 JSON")
    sub.add_parser("list", help="打印看板卡片(纯文本)")
    p_launch = sub.add_parser("launch", help="新建 Claude 窗口")
    p_launch.add_argument("cwd")
    p_launch.add_argument("prompt", nargs="?", default=None)
    p_imp = sub.add_parser("import", help="用 --resume 导入现有会话")
    p_imp.add_argument("sid")
    args = ap.parse_args()
    cmd = args.cmd or "up"
    if cmd == "up":
        return cmd_up()
    if cmd == "board":
        return cmd_board()
    if cmd == "status":
        return cmd_status()
    if cmd == "list":
        return cmd_list()
    if cmd == "launch":
        return cmd_launch(args.cwd, args.prompt)
    if cmd == "import":
        return cmd_import(args.sid)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
