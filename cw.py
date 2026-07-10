#!/usr/bin/env python3
"""cw — Claude 多终端调度器 (claude-wekan).

tmux 当终端复用骨架,curses 看板做调度层。纯 Python 标准库,无 pip/npm。
每个 Claude 窗口都是 [看板 30% │ 会话 55% │ 服务 15%]:左侧常驻实时状态,
中间是对话,右侧显示活跃服务;切会话只是切窗口,左/右始终可见。
看板支持鼠标:点选卡片、再点/双击切换、滚轮移动(tmux 侧仅对 cw 会话开 mouse)。

用法:
  cw up              起/连 tmux 会话 cw,切成 [看板│会话│服务] 并绑定热键,自动启动悬浮看板
  cw board           运行看板 TUI(窗格内或普通终端里都可)
  cw services        运行服务面板 TUI(右侧窄栏)
  cw launch <cwd>    新建一个 [看板│会话│服务] 窗口跑 claude(可选初始 prompt)
  cw import <sid>    用 claude --resume <sid> 把现有会话接进 tmux
  cw pane board|claude|services  聚焦/召出当前窗口的某个窗格(热键内部用)
  cw hud             启动 macOS 原生悬浮看板(也可通过 Ctrl-b h 触发)
  cw status          打印发现的会话/作业(JSON,调试用)
  cw list            打印看板卡片(纯文本)

热键:
  Ctrl-b b    聚焦看板
  Ctrl-b B    聚焦会话
  Ctrl-b s    聚焦服务面板
  Ctrl-b h    启动/聚焦悬浮看板(HUD)
  Ctrl-b N    新建 Claude 会话
"""
import os, sys, json, glob, subprocess, time, re, shlex, curses, argparse

SESSION_NAME = "cw"
SID8_RE = re.compile(r"-(?P<sid>[0-9a-f]{8})$")
SCRIPT = os.path.realpath(__file__)

# 左中右分栏:左看板(30%) / 中会话(55%) / 右服务(15%)。窗格标题用于在窗口里定位。
BOARD_PANE_TITLE = "cw·board"
CLAUDE_PANE_TITLE = "cw·claude"
SERVICES_PANE_TITLE = "cw·services"
BOARD_WIDTH_PCT = "30"
SERVICES_WIDTH_PCT = "22"  # 占剩余70%的22% ≈ 总宽15%


# --------------------------------------------------------------------------
# 数据源
# --------------------------------------------------------------------------

def expand(p):
    return os.path.expanduser(p)


# 已知配置目录 -> 短标签(顺序即 default_sources 扫描顺序,doubao 在前用于去重)
CONFIG_DIRS = [
    ("~/.claude-doubao", "doubao"),
    ("~/.claude-official", "official"),
    ("~/.claude", "default"),
]


def default_sources():
    out = []
    for p, _ in CONFIG_DIRS:
        b = expand(p)
        if os.path.isdir(b):
            out.append(b)
    return out


def config_label(base):
    """配置目录 -> 短标签(default/doubao/official/...);None 视为 default。"""
    if not base:
        return "default"
    ab = os.path.abspath(expand(base))
    for p, lab in CONFIG_DIRS:
        if os.path.abspath(expand(p)) == ab:
            return lab
    return os.path.basename(ab.rstrip("/")) or "?"


def config_base(label):
    """短标签(default/doubao/official)-> 配置目录绝对路径;未知/None -> 默认 ~/.claude。"""
    if label:
        for p, lab in CONFIG_DIRS:
            if lab == label:
                return expand(p)
    return expand("~/.claude")


# 归档:把不想在看板上看到的 session 收起来。存 sid 列表到一个 JSON 文件。
ARCHIVE_FILE = expand("~/.cw_archived.json")


def load_archived():
    """返回已归档 sid 的集合。"""
    try:
        with open(ARCHIVE_FILE) as f:
            data = json.load(f)
        return set(data if isinstance(data, list) else data.get("archived", []))
    except Exception:
        return set()


def save_archived(sids):
    try:
        with open(ARCHIVE_FILE, "w") as f:
            json.dump(sorted(sids), f)
    except Exception:
        pass


def toggle_archived(sid):
    """归档/取消归档一个 sid,返回操作后是否处于归档状态。"""
    s = load_archived()
    if sid in s:
        s.discard(sid)
        archived = False
    else:
        s.add(sid)
        archived = True
    save_archived(s)
    return archived



def claude_cmd(base, rest=""):
    """构造带 CLAUDE_CONFIG_DIR 的 claude 命令,确保接到正确的配置/模型。
    base 为 None 或默认 ~/.claude 时不加前缀(等价于裸 claude)。"""
    prefix = ""
    if base:
        ab = os.path.abspath(expand(base))
        if ab != os.path.abspath(expand("~/.claude")):
            prefix = "CLAUDE_CONFIG_DIR=%s " % shlex.quote(ab)
    rest = (" " + rest) if rest else ""
    return "%sclaude%s" % (prefix, rest)


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
# tmux 窗格布局:每个 Claude 窗口都是 [看板 30% │ 会话 70%]
# --------------------------------------------------------------------------

def _board_cmd():
    return "python3 %s board" % shlex.quote(SCRIPT)


def _services_cmd():
    return "python3 %s services" % shlex.quote(SCRIPT)


def current_window():
    """当前活动窗格所在的 window id(如 @3);不在 tmux 里返回 None。"""
    rc, out, _ = tmux(["display-message", "-p", "#{window_id}"])
    if rc != 0:
        return None
    return out.strip() or None


def find_pane(win, title):
    """在窗口 win 里找标题含 title 的窗格,返回 pane_id;找不到返回 None。"""
    if not win:
        return None
    rc, out, _ = tmux(["list-panes", "-t", win, "-F", "#{pane_title}\t#{pane_id}"])
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and title in parts[0]:
            return parts[1]
    return None


def create_board_pane(win):
    """在 win 左侧切出一个 30% 宽的看板窗格(不抢焦点),返回 pane_id。"""
    target = find_pane(win, CLAUDE_PANE_TITLE)
    if not target:
        # 老窗口:会话窗格没标题,把当前活动窗格当作会话侧并补上标题
        rc, out, _ = tmux(["list-panes", "-t", win, "-F", "#{pane_active}\t#{pane_id}"])
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0] == "1":
                target = parts[1]
                tmux(["select-pane", "-t", target, "-T", CLAUDE_PANE_TITLE])
                break
    if not target:
        target = win
    rc, out, _ = tmux(["split-window", "-h", "-b", "-p", BOARD_WIDTH_PCT, "-d",
                       "-t", target, "-P", "-F", "#{pane_id}", _board_cmd()])
    if rc != 0:
        return None
    bp = out.strip()
    if bp:
        tmux(["select-pane", "-t", bp, "-T", BOARD_PANE_TITLE])
    return bp


def create_services_pane(win):
    """在 win 右侧切出一个 ~15% 宽的服务面板窗格(不抢焦点),返回 pane_id。"""
    target = find_pane(win, CLAUDE_PANE_TITLE)
    if not target:
        return None
    rc, out, _ = tmux(["split-window", "-h", "-p", SERVICES_WIDTH_PCT, "-d",
                       "-t", target, "-P", "-F", "#{pane_id}", _services_cmd()])
    if rc != 0:
        return None
    sp = out.strip()
    if sp:
        tmux(["select-pane", "-t", sp, "-T", SERVICES_PANE_TITLE])
    return sp


def make_claude_window(cwd, shell_cmd, name, resolve_sid=True):
    """新建一个 [看板 30% │ 会话 55% │ 服务 15%] 窗口并聚焦会话侧。返回 (窗口名, err)。"""
    rc, out, err = tmux(["new-window", "-P", "-F", "#{pane_id}", "-t", SESSION_NAME,
                         "-n", name, "-c", cwd, shell_cmd])
    if rc != 0:
        return None, err
    claude_pane = out.strip()
    # 左侧看板
    rc2, out2, _ = tmux(["split-window", "-h", "-b", "-p", BOARD_WIDTH_PCT, "-d",
                         "-t", claude_pane, "-P", "-F", "#{pane_id}", _board_cmd()])
    bp = out2.strip() if rc2 == 0 else None
    if bp:
        tmux(["select-pane", "-t", bp, "-T", BOARD_PANE_TITLE])
    tmux(["select-pane", "-t", claude_pane, "-T", CLAUDE_PANE_TITLE])
    # 右侧服务面板
    sp = create_services_pane("%s:%s" % (SESSION_NAME, name))
    final = name
    if resolve_sid:
        sid = resolve_new_sid(cwd, timeout=12)
        if sid:
            final = "%s-%s" % (name, sid[:8])
            tmux(["rename-window", "-t", "%s:%s" % (SESSION_NAME, name), final])
    tmux(["select-window", "-t", "%s:%s" % (SESSION_NAME, final)])
    tmux(["select-pane", "-t", claude_pane])
    return final, None


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
            "source": base, "config": config_label(base),
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
            "respawn_flags": j.get("respawnFlags") or [],
            "source": j.get("source"), "config": config_label(j.get("source")),
        })
    return cards


# --------------------------------------------------------------------------
# demo 数据(无需真实会话即可预览,也用于生成截图)
# --------------------------------------------------------------------------

def _demo_card(kind, sid, cwd, name, title, status, group, needs=None,
               todos=None, last_prompt=None, last_asst=None, gitBranch=None,
               managed=False, age_s=0, respawn_flags=None):
    now = int(time.time() * 1000)
    ts = now - age_s * 1000
    tr = {"title": title, "last_prompt": last_prompt, "last_user": last_prompt,
          "last_asst": last_asst, "todos": todos or [], "gitBranch": gitBranch,
          "cwd": cwd, "last_ts": ts}
    return {"kind": kind, "sid": sid, "sid8": sid[:8], "cwd": cwd, "name": name,
            "title": title, "status": status, "alive": group != "done",
            "group": group, "startedAt": ts, "updatedAt": ts, "managed": managed,
            "win": None, "tr": tr, "needs": needs, "respawn_flags": respawn_flags or [],
            "source": None, "config": "default"}


def demo_cards():
    t = lambda c, s: {"content": c, "status": s}
    return [
        _demo_card("interactive", "a1b2c3d4e5f6", "~/projects/web-app", "web-app",
                   "refactor auth flow", "busy", "running", managed=True, age_s=180,
                   last_prompt="refactor the auth flow — extract AuthProvider and add tests",
                   last_asst="I'll start by extracting the AuthProvider component…",
                   todos=[t("Refactor AuthProvider", "completed"),
                          t("Add unit tests", "in_progress"),
                          t("Update docs", "pending")],
                   gitBranch="feat/auth-refactor"),
        _demo_card("bg", "e5f6a7b8c9d0", "~/projects/api-server", "deploy to staging",
                   "deploy to staging", "blocked", "waiting",
                   needs="confirm rollback before deploy?", age_s=120,
                   last_prompt="deploy api-server to staging",
                   respawn_flags=["--agent", "claude", "--permission-mode", "auto", "--model", "opus"]),
        _demo_card("bg", "c9d0e1f2a3b4", "~/projects/docs-site", "add API reference",
                   "add API reference", "blocked", "waiting",
                   needs="which version — v1 or v2?", age_s=300,
                   last_prompt="add API reference docs",
                   respawn_flags=["--agent", "claude", "--permission-mode", "auto"]),
        _demo_card("interactive", "112233445566", "~/projects/mobile-app", "mobile-app",
                   "fix push notif", "idle", "external", age_s=720,
                   last_prompt="fix push notifications on iOS"),
        _demo_card("interactive", "5566778899aa", "~/projects/ml-pipeline", "ml-pipeline",
                   "retrain model", "idle", "external", age_s=3600,
                   last_prompt="retrain the ranking model"),
        _demo_card("interactive", "99aabbccddee", "~/projects/cli-tool", "cli-tool",
                   "add --verbose", "busy", "external", age_s=30,
                   last_prompt="add a --verbose flag"),
        _demo_card("bg", "ddeeff001122", "~/projects/web-app", "write unit tests",
                   "write unit tests", "completed", "done", age_s=3600,
                   last_prompt="write unit tests for auth"),
        _demo_card("bg", "223344556677", "~/projects/scripts", "cleanup backups",
                   "cleanup backups", "failed", "done", age_s=10800,
                   last_prompt="cleanup old backups"),
    ]


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


def cmd_launch(cwd, prompt=None, config=None):
    cwd = os.path.abspath(expand(cwd))
    if not os.path.isdir(cwd):
        print("cwd 不存在: %s" % cwd, file=sys.stderr)
        return 1
    base = config_base(config)
    name = projshort(cwd)[:18]
    rest = shlex.quote(prompt) if prompt else ""
    shell_cmd = claude_cmd(base, rest)
    print("启动 claude @ %s [%s] ..." % (cwd, config_label(base)))
    final, err = make_claude_window(cwd, shell_cmd, name, resolve_sid=True)
    if final is None:
        print("tmux 新窗口失败(先 `cw up`?): %s" % err, file=sys.stderr)
        return 1
    print("已建窗口 %s" % final)
    return 0


def cmd_import(sid):
    base, cwd = find_session(sid)
    if not cwd:
        print("找不到 session %s 的 cwd" % sid, file=sys.stderr)
        return 1
    name = "%s-%s" % (projshort(cwd)[:18], sid[:8])
    final, err = make_claude_window(cwd, claude_cmd(base, "--resume " + shlex.quote(sid)),
                                    name, resolve_sid=False)
    if final is None:
        print("tmux 新窗口失败: %s" % err, file=sys.stderr)
        return 1
    print("已导入 %s @ %s [%s](记得关掉旧裸终端以防冲突)" % (final, cwd, config_label(base)))
    return 0


def cmd_up():
    rc, _, _ = tmux(["has-session", "-t", SESSION_NAME])
    new_session = rc != 0
    if new_session:
        tmux(["new-session", "-d", "-s", SESSION_NAME, "-n", "main"])
        print("已创建 tmux 会话 %s" % SESSION_NAME)
        # 初始窗口切成 [看板 30% │ shell 55% │ 服务 15%];右侧先占位
        rc, out, _ = tmux(["list-panes", "-t", "%s:main" % SESSION_NAME,
                           "-F", "#{pane_id}"])
        init = out.strip().splitlines()[0] if (rc == 0 and out.strip()) else None
        if init:
            tmux(["select-pane", "-t", init, "-T", CLAUDE_PANE_TITLE])
            bp = create_board_pane("%s:main" % SESSION_NAME)
            if bp:
                tmux(["select-pane", "-t", bp])  # 首次落地看板
            # 右侧服务面板
            create_services_pane("%s:main" % SESSION_NAME)
    # Ctrl-b b 聚焦/召出看板(当前窗口没有就切一个);Ctrl-b B 聚焦会话
    tmux(["bind-key", "b", "run-shell", "python3 %s pane board" % SCRIPT])
    tmux(["bind-key", "B", "run-shell", "python3 %s pane claude" % SCRIPT])
    # Ctrl-b s 聚焦/召出服务面板
    tmux(["bind-key", "s", "run-shell", "python3 %s pane services" % SCRIPT])
    # Ctrl-b h 启动/聚焦悬浮看板(HUD)
    tmux(["bind-key", "h", "run-shell", "python3 %s hud" % SCRIPT])
    # 鼠标:仅对 cw 会话开启(点击看板卡片切换、滚轮移动;不影响你别的 tmux 会话)
    tmux(["set-option", "-t", SESSION_NAME, "mouse", "on"])
    rc, _, err = tmux(["bind-key", "N", "command-prompt",
                       "-p", "cwd:", "run-shell 'python3 %s launch \"%%1\"'" % SCRIPT])
    if rc != 0:
        print("绑定 Ctrl-b N 失败: %s" % err, file=sys.stderr)
    print("Ctrl-b b = 看板   Ctrl-b B = 会话   Ctrl-b s = 服务   Ctrl-b h = 悬浮   Ctrl-b N = 新建   (会话: %s,鼠标已开)" % SESSION_NAME)
    # 后台启动 HUD 悬浮窗
    try:
        subprocess.Popen(
            [sys.executable, SCRIPT, "hud"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass  # HUD 启动失败不影响主流程
    if os.environ.get("TMUX"):
        tmux(["switch-client", "-t", SESSION_NAME])
    else:
        os.execvp("tmux", ["tmux", "attach", "-t", SESSION_NAME])
    return 0


def cmd_pane(which):
    """聚焦/召出当前窗口的某个窗格(供 Ctrl-b b/B/s 调用)。"""
    win = current_window()
    if not win:
        print("不在 tmux 里,无法定位窗格", file=sys.stderr)
        return 1
    if which == "board":
        bp = find_pane(win, BOARD_PANE_TITLE) or create_board_pane(win)
        if bp:
            tmux(["select-pane", "-t", bp])
    elif which == "claude":
        cp = find_pane(win, CLAUDE_PANE_TITLE)
        if cp:
            tmux(["select-pane", "-t", cp])
    elif which == "services":
        sp = find_pane(win, SERVICES_PANE_TITLE) or create_services_pane(win)
        if sp:
            tmux(["select-pane", "-t", sp])
    else:
        print("用法: cw pane board|claude|services", file=sys.stderr)
        return 1
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
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)  # 选中高亮条
    curses.init_pair(7, curses.COLOR_RED, bg)                    # failed
    curses.init_pair(8, curses.COLOR_MAGENTA, bg)                # 标题点缀


# 状态 -> (字形, 颜色 pair)。dot 按状态着色,比按分组更直观。
_STATUS = {
    "busy": ("●", 2), "running": ("●", 2),
    "idle": ("○", 3), "blocked": ("?", 3),
    "completed": ("✓", 5), "failed": ("✗", 7),
}


def _status_glyph(c):
    st = c.get("status")
    if st in _STATUS:
        return _STATUS[st]
    if c.get("group") == "external":
        return ("○", 4)
    return ("·", 0)


def _truncate(s, n):
    if not s:
        return ""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:max(0, n - 1)] + "…"


_GROUP_RANK = {g: i for i, (g, _, _) in enumerate(GROUPS)}
_GROUP_COL = {g: col for g, _, col in GROUPS}


def _project_key(c):
    """项目分组键:优先完整 cwd(唯一),回退 projshort。"""
    return c.get("cwd") or projshort(c.get("cwd"))


def _card_recency(c):
    return c.get("updatedAt") or c.get("startedAt") or 0


def _card_sort_key(c):
    """项目内卡片排序:先按分组(waiting→running→external→done),再按新近。"""
    return (_GROUP_RANK.get(c["group"], 99), -_card_recency(c))


def _proj_branch(items):
    for c in items:
        gb = (c.get("tr") or {}).get("gitBranch")
        if gb:
            return gb
    return None


def _fold_default(counts):
    """默认折叠策略:有'在等你'的项目展开,其余折叠。"""
    return counts.get("waiting", 0) == 0


def _build_rows(state):
    """把 cards 组织成待绘制的 rows(两级)。row 类型:
    phdr(项目头,可选) / ghdr(状态组头,不可选) / card(可选) / spacer。
    选中用 sel_id(('card',sid) 或 ('proj',pkey))持久,切视图/折叠都不丢。"""
    view = state.get("view", "project")
    cards = state["cards"]
    rows = []
    if view == "project":
        projs = {}
        for c in cards:
            projs.setdefault(_project_key(c), []).append(c)
        metas = []
        for pk, items in projs.items():
            counts = {}
            for c in items:
                counts[c["group"]] = counts.get(c["group"], 0) + 1
            recency = max(_card_recency(c) for c in items)
            if counts.get("waiting"):
                prank = 0
            elif counts.get("running"):
                prank = 1
            elif counts.get("external"):
                prank = 2
            else:
                prank = 3
            metas.append((pk, items, counts, recency, prank))
        metas.sort(key=lambda m: (m[4], -m[3]))
        for pk, items, counts, recency, prank in metas:
            collapsed = state["folds"].get(pk, _fold_default(counts))
            rows.append({"t": "phdr", "id": ("proj", pk), "pkey": pk,
                         "items": items, "counts": counts, "collapsed": collapsed,
                         "branch": _proj_branch(items)})
            if not collapsed:
                for c in sorted(items, key=_card_sort_key):
                    rows.append({"t": "card", "id": ("card", c["sid"]), "card": c,
                                 "indent": True, "show_proj": False})
            rows.append({"t": "spacer"})
    else:  # status 视图:一级是状态组,卡片显示项目名
        for gkey, gname, gcol in GROUPS:
            items = [c for c in cards if c["group"] == gkey]
            if not items:
                continue
            rows.append({"t": "ghdr", "gkey": gkey, "gname": gname,
                         "gcol": gcol, "n": len(items)})
            for c in sorted(items, key=lambda c: -_card_recency(c)):
                rows.append({"t": "card", "id": ("card", c["sid"]), "card": c,
                             "indent": False, "show_proj": True})
            rows.append({"t": "spacer"})
    state["rows"] = rows
    state["sel_rows"] = [i for i, r in enumerate(rows) if r["t"] in ("card", "phdr")]
    _resolve_sel(state)


def _resolve_sel(state):
    """把 sel_id 解析成 rows 下标 sel_row;找不到就落到第一个可选行。"""
    rows, sel_rows = state["rows"], state["sel_rows"]
    if not sel_rows:
        state["sel_row"] = None
        return
    target = None
    for i in sel_rows:
        if rows[i]["id"] == state.get("sel_id"):
            target = i
            break
    if target is None:
        target = sel_rows[0]
    state["sel_row"] = target
    state["sel_id"] = rows[target]["id"]


def _move(state, delta):
    sel_rows = state["sel_rows"]
    if not sel_rows:
        return
    cur = state.get("sel_row")
    pos = sel_rows.index(cur) if cur in sel_rows else 0
    pos = max(0, min(len(sel_rows) - 1, pos + delta))
    state["sel_row"] = sel_rows[pos]
    state["sel_id"] = state["rows"][sel_rows[pos]]["id"]


def _sel_row_obj(state):
    r = state.get("sel_row")
    if r is None:
        return None
    return state["rows"][r]


def _sel_card(state):
    r = _sel_row_obj(state)
    return r["card"] if r and r["t"] == "card" else None


def _toggle_fold(state, pkey):
    counts = {}
    for c in state["cards"]:
        if _project_key(c) == pkey:
            counts[c["group"]] = counts.get(c["group"], 0) + 1
    cur = state["folds"].get(pkey, _fold_default(counts))
    state["folds"][pkey] = not cur


def _fold_all(state, collapsed):
    for c in state["cards"]:
        state["folds"][_project_key(c)] = collapsed


def _put(stdscr, y, x, s, w, attr=0):
    """安全绘制:按列裁剪(保留空格,不折叠),吞掉越界的 curses.error。返回下一列 x。"""
    if y < 0 or x < 0 or x >= w or not s:
        return x
    s = str(s)
    avail = w - x
    if len(s) > avail:
        s = s[:max(0, avail - 1)] + "…" if avail > 0 else ""
    try:
        stdscr.addnstr(y, x, s, w - x, attr)
    except curses.error:
        pass
    return x + len(s)


def _rollup_chips(counts):
    """项目状态汇总:[(文本, 颜色pair), ...],只列非零、按分组顺序。"""
    out = []
    for gkey, _, gcol in GROUPS:
        n = counts.get(gkey, 0)
        if not n:
            continue
        glyph = {"running": "●", "waiting": "?", "external": "○", "done": "✓"}[gkey]
        out.append(("%s%d" % (glyph, n), gcol))
    return out


def _draw(stdscr, state):
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    narrow = w < 72
    view = state.get("view", "project")
    # ---- header:视图标记 + 分组计数 chip ----
    counts = {g: 0 for g, _, _ in GROUPS}
    for c in state["cards"]:
        counts[c["group"]] = counts.get(c["group"], 0) + 1
    x = _put(stdscr, 0, 0, "claude-wekan", w, curses.color_pair(8) | curses.A_BOLD)
    tag = "项目" if view == "project" else "状态"
    x = _put(stdscr, 0, x, " [%s]" % tag, w, curses.color_pair(5) | curses.A_BOLD)
    x = _put(stdscr, 0, x, "  ", w)
    for gkey, gname, gcol in GROUPS:
        chip = "%s %d" % (gname.split()[0], counts.get(gkey, 0))
        x = _put(stdscr, 0, x, chip + "  ", w,
                 curses.color_pair(gcol) | (curses.A_BOLD if counts.get(gkey) else curses.A_DIM))
    if narrow:
        hint = "j/k ⏎切 g视图 ␣折叠 n新 q→"
    else:
        hint = "j/k·滚轮 移动   ⏎/点 切换·折叠   g 项目/状态   ␣ 折叠   n 新建   q →会话"
    _put(stdscr, 1, 0, hint, w, curses.A_DIM)

    # ---- rows 绘制 ----
    state["row_map"] = {}   # 屏幕行 -> rows 下标(供鼠标命中)
    row = 3
    for ri, r in enumerate(state["rows"]):
        if row >= h - 6:
            break
        t = r["t"]
        if t == "spacer":
            row += 1
            continue
        selected = (ri == state.get("sel_row"))
        if t == "phdr":
            state["row_map"][row] = ri
            _draw_proj_row(stdscr, row, r, w, selected, narrow)
            row += 1
        elif t == "ghdr":
            bar = " %s (%d) " % (r["gname"], r["n"])
            bar = "─" + bar + "─" * max(0, w - len(bar) - 2)
            _put(stdscr, row, 0, bar, w, curses.color_pair(r["gcol"]) | curses.A_BOLD)
            row += 1
        elif t == "card":
            state["row_map"][row] = ri
            _draw_card_row(stdscr, row, r, w, selected, narrow)
            row += 1
    # ---- footer:选中详情 ----
    sel = _sel_row_obj(state)
    if sel and row < h:
        _put(stdscr, min(row, h - 6), 0, "─" * w, w, curses.A_DIM)
        if sel["t"] == "card":
            _draw_detail(stdscr, sel["card"], min(row, h - 6) + 1, w, h)
        elif sel["t"] == "phdr":
            _draw_proj_detail(stdscr, sel, min(row, h - 6) + 1, w, h)


def _draw_proj_row(stdscr, row, r, w, selected, narrow):
    base = curses.color_pair(6) | curses.A_BOLD if selected else 0
    if selected:
        try:
            stdscr.addnstr(row, 0, " " * w, w, base)
        except curses.error:
            pass
    tri = "▾" if not r["collapsed"] else "▸"
    x = _put(stdscr, row, 0, "%s " % tri, w, base or curses.A_BOLD)
    name = _truncate(projshort(r["pkey"]), 20 if not narrow else 14)
    x = _put(stdscr, row, x, "%-*s " % (20 if not narrow else 14, name), w,
             base or curses.A_BOLD)
    # 汇总 chip
    for txt, col in _rollup_chips(r["counts"]):
        x = _put(stdscr, row, x, txt + " ", w, base or (curses.color_pair(col) | curses.A_BOLD))
    # 分支右对齐(宽屏)
    if not narrow and r.get("branch"):
        b = _truncate(r["branch"], 22)
        _put(stdscr, row, max(x + 1, w - len(b) - 1), b, w, base or curses.A_DIM)


def _draw_card_row(stdscr, row, r, w, selected, narrow):
    c = r["card"]
    base = curses.color_pair(6) | curses.A_BOLD if selected else 0
    if selected:
        try:
            stdscr.addnstr(row, 0, " " * w, w, base)
        except curses.error:
            pass
    glyph, gc = _status_glyph(c)
    ag = age_str(_card_recency(c))
    indent = "    " if r.get("indent") else " "
    mark = "▸" if selected else " "
    x = _put(stdscr, row, 0, indent[:-1] + mark + " ", w, base)
    x = _put(stdscr, row, x, glyph, w, base if selected else (curses.color_pair(gc) | curses.A_BOLD))
    x = _put(stdscr, row, x, " ", w, base)
    cfg = c.get("config")
    if cfg and cfg != "default":
        x = _put(stdscr, row, x, "[%s] " % cfg, w, base or (curses.color_pair(4) | curses.A_BOLD))
    if r.get("show_proj"):
        proj = _truncate(projshort(c.get("cwd")), 14 if not narrow else 10)
        x = _put(stdscr, row, x, "%-*s " % (14 if not narrow else 10, proj), w,
                 base or curses.A_BOLD)
    title = c.get("title") or c.get("name")
    # waiting 卡直接把 needs 顶到前面(painkiller)
    needs = c.get("needs") if c.get("group") == "waiting" else None
    if narrow:
        body = _truncate(title, max(4, w - x - len(ag) - 2))
        _put(stdscr, row, x, body, w, base)
    else:
        title = _truncate(title, 24)
        x = _put(stdscr, row, x, "%-25s " % title, w, base)
        td = todo_summary(c.get("tr", {}).get("todos"))
        if td:
            x = _put(stdscr, row, x, "☑%s " % td, w, base or curses.color_pair(2))
        if needs:
            tail = _truncate("needs: " + needs, max(0, w - x - len(ag) - 2))
            x = _put(stdscr, row, x, tail, w, base or curses.color_pair(3))
        else:
            task = _truncate(c.get("tr", {}).get("last_prompt") or c.get("tr", {}).get("last_user"),
                             max(0, w - x - len(ag) - 2))
            x = _put(stdscr, row, x, task, w, base or curses.A_DIM)
    if ag:
        _put(stdscr, row, max(x + 1, w - len(ag) - 1), ag, w, base or curses.A_DIM)


def _draw_proj_detail(stdscr, r, top, w, h):
    """折叠项目头选中时的详情:列出该项目下的会话摘要。"""
    items = sorted(r["items"], key=_card_sort_key)
    _put(stdscr, top, 0, _truncate("项目 %s — %d 个会话" %
                                   (projshort(r["pkey"]), len(items)), w), w, curses.A_BOLD)
    rr = top + 1
    for c in items:
        if rr >= h:
            break
        glyph, gc = _status_glyph(c)
        line = "%s %s" % (glyph, _truncate(c.get("title") or c.get("name"), w - 6))
        if c.get("group") == "waiting" and c.get("needs"):
            line += "  · needs: " + c["needs"]
        _put(stdscr, rr, 2, _truncate(line, w - 2), w, curses.color_pair(gc))
        rr += 1


def _draw_detail(stdscr, c, top, w, h):
    tr = c.get("tr", {}) or {}
    lines = []
    lines.append(("name", c.get("name")))
    lines.append(("config", c.get("config")))
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
    """处理选中行:项目头→折叠展开;卡片→切窗口/导入/回复。返回 False(看板常驻)。"""
    r = _sel_row_obj(state)
    if r is None:
        return False
    if r["t"] == "phdr":
        _toggle_fold(state, r["pkey"])
        return False
    if state.get("demo"):
        state["msg"] = "demo 模式 —— 跑 `cw up` 进入真实会话"
        return False
    c = r["card"]
    if c["managed"] and c["win"]:
        win = "%s:%s" % (SESSION_NAME, c["win"])
        tmux(["select-window", "-t", win])
        cp = find_pane(win, CLAUDE_PANE_TITLE)
        if cp:
            tmux(["select-pane", "-t", cp])
        return False
    if c["group"] == "external" and c.get("sid"):
        # 导入并切换(带上该会话所属配置,避免切错模型)
        name = "%s-%s" % (projshort(c.get("cwd"))[:18], c["sid8"])
        cwd = c.get("cwd") or "."
        final, err = make_claude_window(
            cwd, claude_cmd(c.get("source"), "--resume " + shlex.quote(c["sid"])),
            name, resolve_sid=False)
        if final is not None:
            return False
        state["msg"] = "导入失败: %s" % err
        return False
    if c["kind"] == "bg" and c["group"] == "waiting" and c.get("needs"):
        return _reply(stdscr, state, c)
    return False


def _reply(stdscr, state, c):
    """给被阻塞的后台 agent 发回复:内嵌输入 → claude --resume <sid> <flags> -p <reply>
    在新 [看板│会话] 窗口跑(响应流可见)→ 聚焦会话侧。看板保持运行(返回 False)。"""
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    stdscr.addnstr(0, 0, "回复后台 agent(Enter 发送 / Esc 取消)", w, curses.A_BOLD)
    stdscr.addnstr(2, 0, _truncate("Q: %s" % c.get("needs"), w - 2), w, curses.color_pair(3))
    stdscr.addnstr(3, 0, _truncate("agent: %s" % c.get("name"), w - 2), w, curses.A_DIM)
    reply = _edit_line(stdscr, h - 2, 0, ">> ", "", w)
    if not reply:
        return False
    sid = c.get("sid")
    cwd = c.get("cwd") or "."
    flags = " ".join(shlex.quote(f) for f in (c.get("respawn_flags") or []))
    # --resume <sid> + 原始 flags + -p <回复>(headless 一次性发送,响应打印到窗口)
    rest = "--resume %s %s -p %s" % (shlex.quote(sid), flags, shlex.quote(reply))
    cmd = claude_cmd(c.get("source"), rest)
    name = "reply-%s" % c.get("sid8")
    final, err = make_claude_window(cwd, cmd, name, resolve_sid=False)
    if final is None:
        state["msg"] = "回复失败(先 `cw up`?): %s" % err
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
    # 可选配置(仅列出真实存在的目录)
    configs = [lab for p, lab in CONFIG_DIRS if os.path.isdir(expand(p))]
    stdscr.erase()
    stdscr.addnstr(0, 0, "新建 Claude 会话(Enter 确认 / Esc 取消)", w, curses.A_BOLD)
    for i, p in enumerate(known[:10]):
        stdscr.addnstr(2 + i, 2, "%d. %s" % (i + 1, _truncate(p, w - 6)), w, curses.A_DIM)
    cwd = _edit_line(stdscr, h - 5, 0, "cwd: ", default_cwd, w)
    if not cwd:
        return
    config = None
    if len(configs) > 1:
        hint = "config(%s): " % "/".join(configs)
        config = _edit_line(stdscr, h - 4, 0, hint, configs[0], w)
        if config is None:
            return
        config = config.strip() or configs[0]
    base = config_base(config)
    prompt = _edit_line(stdscr, h - 2, 0, "prompt(可空): ", "", w)
    cwd = os.path.abspath(expand(cwd))
    name = projshort(cwd)[:18]
    shell_cmd = claude_cmd(base, shlex.quote(prompt) if prompt else "")
    final, err = make_claude_window(cwd, shell_cmd, name, resolve_sid=True)
    if final is None:
        state["msg"] = "新建失败: %s" % err
        return
    state["msg"] = "已新建 @ %s [%s]" % (cwd, config_label(base))
    state["cards"] = gather_cards()


def _board_main(stdscr, demo=False):
    _init_colors()
    curses.curs_set(0)
    # 开启鼠标:点击选中/切换 + 滚轮移动。tmux 侧已 `mouse on`,SGR 事件传进来。
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        curses.mouseinterval(0)  # 拿原始 press/release,双击由我们自己按时间判定
    except curses.error:
        pass
    curses.halfdelay(30)  # 3s 超时 -> 自动刷新
    state = {"cards": (demo_cards() if demo else gather_cards()),
             "view": "project", "folds": {}, "rows": [], "sel_rows": [],
             "sel_row": None, "sel_id": None, "msg": "", "demo": demo,
             "row_map": {}, "last_click": (None, 0.0)}
    while True:
        _build_rows(state)
        try:
            _draw(stdscr, state)
            if state.get("msg"):
                h, w = stdscr.getmaxyx()
                _put(stdscr, h - 1, 0, state["msg"], w, curses.color_pair(3))
        except curses.error:
            pass  # 窗口过小时忽略绘制错误,不崩溃
        ch = stdscr.getch()
        if ch == -1:
            if not demo:
                state["cards"] = gather_cards()
            continue
        if ch == curses.KEY_MOUSE:
            _handle_mouse(state, stdscr)
            continue
        if ch in (ord("q"),):
            # 在 tmux 看板窗格里:聚焦右侧会话(demo / 裸终端则退出)
            if demo:
                break
            cp = find_pane(current_window(), CLAUDE_PANE_TITLE)
            if cp:
                tmux(["select-pane", "-t", cp])
            else:
                break
        elif ch == ord("r"):
            if not demo:
                state["cards"] = gather_cards()
        elif ch == ord("g"):
            state["view"] = "status" if state.get("view") == "project" else "project"
        elif ch in (ord("z"),):
            # 全折叠 / 全展开 切换:任一项目展开中 -> 全折叠;否则全展开
            any_open = any(r["t"] == "phdr" and not r["collapsed"] for r in state["rows"])
            _fold_all(state, any_open)
        elif ch in (curses.KEY_DOWN, ord("j")):
            _move(state, 1)
        elif ch in (curses.KEY_UP, ord("k")):
            _move(state, -1)
        elif ch in (10, 13, curses.KEY_ENTER, ord(" ")):
            _activate(state, stdscr)
        elif ch == ord("n"):
            if demo:
                state["msg"] = "demo 模式 —— 跑 `cw up` 进入真实会话"
            else:
                _new_session(stdscr, state)
        elif ch == ord("i"):
            c = _sel_card(state)
            if c and c.get("group") == "external":
                _activate(state, stdscr)
            else:
                state["msg"] = "选中一个 EXTERNAL 会话再用 i 导入"


def _handle_mouse(state, stdscr):
    """鼠标:滚轮上下移动;点击项目头 -> 折叠展开;点击卡片 -> 未选中则选中,
    已选中(或双击)则打开。只认按下,忽略松开,避免一次点击触发两次。"""
    try:
        _id, mx, my, _z, bstate = curses.getmouse()
    except curses.error:
        return
    # 滚轮
    if bstate & getattr(curses, "BUTTON4_PRESSED", 0):
        _move(state, -1)
        return
    if bstate & getattr(curses, "BUTTON5_PRESSED", 0):
        _move(state, 1)
        return
    press = getattr(curses, "BUTTON1_PRESSED", 0)
    clicked = curses.BUTTON1_CLICKED
    dbl = getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
    if not (bstate & (press | clicked | dbl)):
        return
    ri = state.get("row_map", {}).get(my)
    if ri is None:
        return
    row = state["rows"][ri]
    rid = row["id"]
    now = time.time()
    last_id, last_t = state.get("last_click", (None, 0.0))
    is_double = bool(bstate & dbl) or (last_id == rid and (now - last_t) < 0.4)
    state["last_click"] = (rid, now)
    # 点项目头:直接折叠展开;点卡片:选中,再点/双击才打开
    state["sel_row"] = ri
    state["sel_id"] = rid
    if row["t"] == "phdr":
        _toggle_fold(state, row["pkey"])
    elif is_double or last_id == rid:
        _activate(state, stdscr)


def cmd_board(demo=False):
    try:
        curses.wrapper(lambda stdscr: _board_main(stdscr, demo))
    except curses.error as e:
        print("curses 错误(终端太小?): %s" % e, file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
# 服务面板 TUI(右侧窄栏,显示运行中的会话/任务)
# --------------------------------------------------------------------------

def _services_main(stdscr):
    """服务面板:显示所有活跃的会话和后台任务,紧凑列表。"""
    _init_colors()
    curses.curs_set(0)
    curses.halfdelay(30)  # 3s 超时 -> 自动刷新
    while True:
        cards = gather_cards()
        # 只保留活跃的(非 done)
        active = [c for c in cards if c["group"] != "done"]
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        # 标题
        _put(stdscr, 0, 0, "服务", w, curses.color_pair(8) | curses.A_BOLD)
        count_text = "%d 活跃" % len(active)
        _put(stdscr, 0, 6, count_text, w, curses.A_DIM)
        # 分隔线
        _put(stdscr, 1, 0, "─" * w, w, curses.A_DIM)
        # 列表
        row = 2
        for c in active[:h - 4]:  # 留几行底部空间
            glyph, gc = _status_glyph(c)
            proj = _truncate(projshort(c.get("cwd")), 10)
            title = _truncate(c.get("title") or c.get("name"), w - 14)
            cfg = c.get("config")
            cfg_tag = "[%s] " % cfg if cfg and cfg != "default" else ""
            line = "%s%s %s" % (cfg_tag, proj, title)
            _put(stdscr, row, 0, glyph, w, curses.color_pair(gc) | curses.A_BOLD)
            _put(stdscr, row, 2, _truncate(line, w - 2), w, curses.A_DIM)
            row += 1
        if not active:
            _put(stdscr, 3, 0, "(无活跃服务)", w, curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("q"),):
            # q: 聚焦回会话窗格
            cp = find_pane(current_window(), CLAUDE_PANE_TITLE)
            if cp:
                tmux(["select-pane", "-t", cp])
            else:
                break


def cmd_services():
    try:
        curses.wrapper(_services_main)
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


def _list_card_line(c):
    tr = c.get("tr", {}) or {}
    stat = c.get("status") or "?"
    proj = projshort(c.get("cwd"))
    title = _truncate(tr.get("title") or c.get("name"), 30)
    task = _truncate(tr.get("last_prompt") or tr.get("last_user"), 50)
    td = todo_summary(tr.get("todos"))
    ag = age_str(c.get("updatedAt") or c.get("startedAt"))
    mkind = "tmux" if c.get("managed") else ("bg" if c.get("kind") == "bg" else "ext")
    cfg = c.get("config") or "default"
    print("  [%-7s] %-8s %-16s %-30s %-4s  %s%s" % (stat, cfg, proj, title, mkind,
                                             ("☑%s " % td if td else ""), ag))
    if task:
        print("             task: %s" % task)
    if c.get("needs"):
        print("             needs: %s" % _truncate(c["needs"], 70))


def cmd_list(demo=False, by="project"):
    cards = demo_cards() if demo else gather_cards()
    if not cards:
        print("(没发现任何 Claude 会话)")
        return 0
    if by == "status":
        for gkey, gname, _ in GROUPS:
            items = [c for c in cards if c["group"] == gkey]
            if not items:
                continue
            print("\n── %s (%d) ──" % (gname, len(items)))
            for c in sorted(items, key=lambda c: -_card_recency(c)):
                _list_card_line(c)
        return 0
    # 项目视图:一级项目(附状态汇总),二级会话
    projs = {}
    for c in cards:
        projs.setdefault(_project_key(c), []).append(c)
    def prank(items):
        counts = {}
        for c in items:
            counts[c["group"]] = counts.get(c["group"], 0) + 1
        for i, g in enumerate(("waiting", "running", "external", "done")):
            if counts.get(g):
                return i
        return 9
    for pk in sorted(projs, key=lambda k: (prank(projs[k]),
                                           -max(_card_recency(c) for c in projs[k]))):
        items = projs[pk]
        counts = {}
        for c in items:
            counts[c["group"]] = counts.get(c["group"], 0) + 1
        roll = " ".join("%s%d" % (t, counts[g]) for g, t in
                        (("running", "●"), ("waiting", "?"), ("external", "○"), ("done", "✓"))
                        if counts.get(g))
        branch = _proj_branch(items)
        print("\n▾ %s  %s%s" % (projshort(pk), roll,
                                ("   [%s]" % branch if branch else "")))
        for c in sorted(items, key=_card_sort_key):
            _list_card_line(c)
    return 0


# --------------------------------------------------------------------------
# HUD:macOS 原生悬浮看板(PyObjC NSPanel,浅色毛玻璃,卡片瓷砖,按项目分区)
# --------------------------------------------------------------------------

# 状态点颜色(RGB 0..1),对应终端配色
HUD_DOT = {
    "busy": (0.30, 0.78, 0.36), "running": (0.30, 0.78, 0.36),   # 绿
    "idle": (0.95, 0.72, 0.16), "blocked": (0.95, 0.72, 0.16),   # 黄
    "completed": (0.28, 0.72, 0.85), "failed": (0.90, 0.30, 0.30),  # 青 / 红
}


def _hud_dot(c):
    st = c.get("status")
    if st in HUD_DOT:
        return HUD_DOT[st]
    if c.get("group") == "external":
        return (0.40, 0.55, 0.95)   # 蓝
    return (0.55, 0.55, 0.55)


def hud_projects(cards=None, archived_view=False):
    """把卡片按项目聚合成 GUI 可直接消费的结构(无 curses / AppKit 依赖)。
    返回 [{'project','branch','cards':[card_lite,...]}],项目内 waiting 优先、
    再按最近活跃;项目之间:含 waiting 的项目排前,再按项目最近活跃。
    archived_view=False 时隐藏已归档的 session;=True 时只显示已归档的。"""
    cards = gather_cards() if cards is None else cards
    archived = load_archived()
    cards = [c for c in cards
             if (c.get("sid") in archived) == archived_view]
    groups = {}
    for c in cards:
        groups.setdefault(_project_key(c), []).append(c)

    def card_lite(c):
        tr = c.get("tr", {}) or {}
        r, g, b = _hud_dot(c)
        return {
            "sid": c.get("sid"), "sid8": c.get("sid8"),
            "name": _truncate(tr.get("title") or c.get("name"), 22),
            "config": c.get("config") or "default",
            "task": _truncate(tr.get("last_prompt") or tr.get("last_user"), 40),
            "needs": _truncate(c.get("needs"), 40) if c.get("group") == "waiting" else None,
            "todo": todo_summary(tr.get("todos")),
            "group": c.get("group"), "win": c.get("win"),
            "managed": c.get("managed"), "dot": (r, g, b),
        }

    out = []
    for pk, items in groups.items():
        items = sorted(items, key=_card_sort_key)
        out.append({
            "project": projshort(pk),
            "branch": _proj_branch(items),
            "waiting": any(x.get("group") == "waiting" for x in items),
            "recency": max((_card_recency(x) for x in items), default=0),
            "cards": [card_lite(c) for c in items],
        })
    out.sort(key=lambda p: (0 if p["waiting"] else 1, -p["recency"]))
    return out


def _hud_focus_card(card):
    """点卡片:切到对应 tmux 窗口,并把终端拉回前台。"""
    win = card.get("win")
    if win:
        tmux(["select-window", "-t", "%s:%s" % (SESSION_NAME, win)])
        tmux(["switch-client", "-t", SESSION_NAME])
    _hud_set_terminal(True)


def _hud_set_terminal(show):
    """显示(激活)或隐藏运行 cw 的终端 app。"""
    tp = os.environ.get("TERM_PROGRAM")
    app_name = {"iTerm.app": "iTerm", "Apple_Terminal": "Terminal"}.get(tp, "Terminal")
    proc_name = {"iTerm.app": "iTerm2", "Apple_Terminal": "Terminal"}.get(tp, "Terminal")
    if show:
        _hud_show_session(app_name)
    else:
        script = ('tell application "System Events" to set visible of '
                  'process "%s" to false' % proc_name)
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=3)
        except Exception:
            pass


def _hud_show_session(app_name="Terminal"):
    """确保 cw 会话的 [看板│会话] 布局展现在前台。
    cw 会话不存在 → 什么都不做;有客户端连着 → 只激活终端 app;
    没有客户端(裸会话)→ 新开一个 Terminal 窗口 attach。"""
    rc, _, _ = tmux(["has-session", "-t", SESSION_NAME])
    if rc != 0:
        return  # 没有 cw 会话,交给用户自己 cw up
    rc, out, _ = tmux(["list-clients", "-t", SESSION_NAME, "-F", "#{client_tty}"])
    ttys = [t for t in out.strip().splitlines() if t] if rc == 0 else []
    if ttys:
        # 已有终端连着 cw:精确把那个 tty 对应的窗口/标签页带到前台
        # (直接 activate 只会拉起最近用过的窗口,可能是跑 hud 的裸终端)
        tty = ttys[0]
        if app_name == "Terminal":
            script = ('tell application "Terminal"\n'
                      '  activate\n'
                      '  repeat with w in windows\n'
                      '    repeat with t in tabs of w\n'
                      '      if tty of t is "%s" then\n'
                      '        set selected of t to true\n'
                      '        set frontmost of w to true\n'
                      '        return\n'
                      '      end if\n'
                      '    end repeat\n'
                      '  end repeat\n'
                      'end tell') % tty
        else:
            # iTerm 尽力而为:仅激活应用
            script = 'tell application "%s" to activate' % app_name
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
        except Exception:
            pass
    else:
        # 裸会话,新开窗口 attach
        cmd = "tmux attach -t %s" % SESSION_NAME
        if app_name == "iTerm":
            script = ('tell application "iTerm" to create window with '
                      'default profile command "%s"' % cmd)
        else:
            script = 'tell application "Terminal" to do script "%s"' % cmd
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
            subprocess.run(["osascript", "-e",
                            'tell application "%s" to activate' % app_name],
                           capture_output=True, timeout=3)
        except Exception:
            pass


def cmd_hud(config=None):
    try:
        import objc  # noqa: F401
        from AppKit import (
            NSApplication, NSPanel, NSView, NSColor, NSFont,
            NSVisualEffectView, NSScreen, NSMakeRect, NSTimer,
            NSApplicationActivationPolicyAccessory,
            NSWindowStyleMaskBorderless, NSWindowStyleMaskResizable,
            NSFloatingWindowLevel, NSTrackingArea,
            NSVisualEffectMaterialHUDWindow, NSVisualEffectBlendingModeBehindWindow,
            NSVisualEffectStateActive, NSBezierPath, NSMutableParagraphStyle,
            NSButton, NSMenu, NSMenuItem, NSEvent,
        )
        from Foundation import NSObject, NSMakePoint
    except Exception as e:
        print("cw hud 需要 PyObjC:  pip install pyobjc", file=sys.stderr)
        print("(%s)" % e, file=sys.stderr)
        return 1

    # 让 Ctrl-C 能中断 app.run()(否则被 Cocoa 事件循环吞掉)
    import signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    RADIUS = 14.0
    PAD = 14.0
    TILE_W, TILE_H = 168.0, 92.0
    GAP = 10.0
    MIN_H = 30.0  # 最小化后只留标题条的高度
    _HUD_DELEG = [None]  # 让 TileView 能拿到 delegate(重建视图用)

    def color(r, g, b, a=1.0):
        return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)

    class TileView(NSView):
        """单张会话瓷砖:白底圆角 + 顶部状态色条 + 文本。点击→切窗回终端。"""
        def initWithCard_(self, card):
            self = objc.super(TileView, self).init()
            if self is None:
                return None
            self._card = card
            return self

        def isFlipped(self):
            return True

        def drawRect_(self, rect):
            b = self.bounds()
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(b, 8, 8)
            color(1, 1, 1, 0.92).setFill()
            path.fill()
            # 顶部状态色条
            r, g, bl = self._card["dot"]
            bar = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(0, 0, b.size.width, 4), 2, 2)
            color(r, g, bl, 1).setFill()
            bar.fill()

        def mouseDown_(self, ev):
            _hud_focus_card(self._card)

        def rightMouseDown_(self, ev):
            menu = NSMenu.alloc().init()
            is_arch = self._card.get("sid") in load_archived()
            title = "取消归档" if is_arch else "归档"
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, "archiveCard:", "")
            item.setTarget_(self)
            menu.addItem_(item)
            NSMenu.popUpContextMenu_withEvent_forView_(menu, ev, self)

        def archiveCard_(self, sender):
            sid = self._card.get("sid")
            if sid:
                toggle_archived(sid)
                _HUD_DELEG[0].view.rebuild()

    def label(text, x, y, w, h, size, rgb, bold=False, dim=False):
        from AppKit import NSTextField
        tf = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        tf.setStringValue_(text or "")
        tf.setBezeled_(False)
        tf.setDrawsBackground_(False)
        tf.setEditable_(False)
        tf.setSelectable_(False)
        f = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
        tf.setFont_(f)
        r, g, b = rgb
        tf.setTextColor_(color(r, g, b, 0.55 if dim else 0.92))
        tf.setLineBreakMode_(4)  # truncating tail
        return tf

    def build_tile(card):
        v = TileView.alloc().initWithCard_(card)
        v.setFrame_(NSMakeRect(0, 0, TILE_W, TILE_H))
        ink = (0.12, 0.12, 0.14)
        # 会话名(粗)
        v.addSubview_(label("● " + card["name"], 8, 8, TILE_W - 16, 18, 12, ink, bold=True))
        # config 徽章
        cfg = card["config"]
        badge = {"doubao": (0.55, 0.35, 0.85), "official": (0.20, 0.55, 0.80)}.get(
            cfg, (0.45, 0.45, 0.45))
        v.addSubview_(label("[%s]" % cfg, 8, 28, TILE_W - 16, 14, 10, badge, bold=True))
        # 任务 / needs
        if card["needs"]:
            v.addSubview_(label("↳ " + card["needs"], 8, 46, TILE_W - 16, 30,
                                10, (0.85, 0.45, 0.10)))
        else:
            v.addSubview_(label(card["task"], 8, 46, TILE_W - 16, 30, 10, ink, dim=True))
        # 进度
        if card["todo"]:
            v.addSubview_(label("☑ " + card["todo"], 8, TILE_H - 20, 60, 14, 10,
                                (0.30, 0.60, 0.35)))
        return v

    class HUDView(NSView):
        """内容视图:自绘拖动(标题区),托管所有瓷砖布局。"""
        def initWithFrame_(self, frame):
            self = objc.super(HUDView, self).initWithFrame_(frame)
            if self is None:
                return None
            self._drag = None
            return self

        def isFlipped(self):
            return True

        def acceptsFirstResponder(self):
            return True

        def keyDown_(self, ev):
            # Esc(53)或 q 退出
            if ev.keyCode() == 53 or ev.charactersIgnoringModifiers() == "q":
                app.terminate_(None)
            else:
                objc.super(HUDView, self).keyDown_(ev)

        def rebuild(self):
            for sub in list(self.subviews()):
                sub.removeFromSuperview()
            b = self.bounds()
            ink = (0.10, 0.10, 0.12)
            arch = bool(_HUD_DELEG[0] and _HUD_DELEG[0].archived_view)
            # 标题
            head = "cw · 归档" if arch else "cw · 悬浮看板"
            self.addSubview_(label(head, PAD, 6, b.size.width - 2 * PAD, 18,
                                   12, ink, bold=True))
            projs = hud_projects(archived_view=arch)
            cols = max(1, int((b.size.width - PAD) // (TILE_W + GAP)))
            y = 30.0
            if not projs:
                empty = "(归档区为空)" if arch else "(没有会话)"
                self.addSubview_(label(empty, PAD, y, b.size.width - 2 * PAD, 16,
                                       11, (0.5, 0.5, 0.5)))
                return
            for p in projs:
                title = p["project"] + ("  [%s]" % p["branch"] if p["branch"] else "")
                self.addSubview_(label(title, PAD, y, b.size.width - 2 * PAD, 16,
                                       11, (0.35, 0.35, 0.40), bold=True))
                y += 20
                for i, card in enumerate(p["cards"]):
                    row, col = divmod(i, cols)
                    tv = build_tile(card)
                    tv.setFrameOrigin_(NSMakePoint(
                        PAD + col * (TILE_W + GAP),
                        y + row * (TILE_H + GAP)))
                    self.addSubview_(tv)
                rows = (len(p["cards"]) + cols - 1) // cols
                y += rows * (TILE_H + GAP) + 8

        # ---- 拖动窗口(点标题区空白拖动整窗)----
        def mouseDown_(self, ev):
            self._drag = ev.locationInWindow()

        def mouseDragged_(self, ev):
            if self._drag is None:
                return
            win = self.window()
            wf = win.frame()
            loc = win.convertRectToScreen_(NSMakeRect(
                ev.locationInWindow().x, ev.locationInWindow().y, 0, 0)).origin
            base = win.convertRectToScreen_(NSMakeRect(
                self._drag.x, self._drag.y, 0, 0)).origin
            win.setFrameOrigin_(NSMakePoint(wf.origin.x + (loc.x - base.x),
                                            wf.origin.y + (loc.y - base.y)))

        def mouseUp_(self, ev):
            self._drag = None

    class Delegate(NSObject):
        def refresh_(self, timer):
            if not self.minimized:
                self.view.rebuild()

        def quit_(self, sender):
            app.terminate_(None)

        def toggleArchived_(self, sender):
            self.archived_view = not self.archived_view
            sender.setTitle_("看板" if self.archived_view else "归档区")
            self.view.rebuild()

        def _set_collapsed(self, collapse):
            """收成标题条 / 展开回记忆尺寸。self.minimized 记录当前态。"""
            if collapse == self.minimized:
                return
            f = self.panel.frame()
            top = f.origin.y + f.size.height  # 顶边固定
            if collapse:
                self.expandedH = f.size.height
                newH = MIN_H
                self.minimized = True
                self.minBtn.setTitle_("+")
            else:
                newH = self.expandedH
                self.minimized = False
                self.minBtn.setTitle_("–")
                self.view.rebuild()
            self.panel.setFrame_display_animate_(
                NSMakeRect(f.origin.x, top - newH, f.size.width, newH), True, True)

        def cycleMode_(self, sender):
            # 三态循环: 0 悬浮  1 终端  2 并存
            self.mode = (self.mode + 1) % 3
            if self.mode == 0:      # 仅悬浮看板
                self._set_collapsed(False)
                _hud_set_terminal(False)
                sender.setTitle_("悬浮")
            elif self.mode == 1:    # 仅 tmux 终端(HUD 收成条)
                self._set_collapsed(True)
                _hud_set_terminal(True)
                sender.setTitle_("终端")
            else:                   # 两者并存
                self._set_collapsed(False)
                _hud_set_terminal(True)
                sender.setTitle_("并存")

        def toggle_(self, sender):
            self._set_collapsed(not self.minimized)

    class KeyPanel(NSPanel):
        # borderless 窗口默认不能成为 key window,重写后才能收键盘事件
        def canBecomeKeyWindow(self):
            return True

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    scr = NSScreen.mainScreen().frame()
    W, H = 560.0, 420.0
    x = scr.origin.x + (scr.size.width - W) / 2
    y = scr.origin.y + scr.size.height - H - scr.size.height * 0.08
    style = NSWindowStyleMaskBorderless | NSWindowStyleMaskResizable
    panel = KeyPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, y, W, H), style, 2, False)
    panel.setLevel_(NSFloatingWindowLevel)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setMovableByWindowBackground_(False)
    panel.setHasShadow_(True)
    panel.setHidesOnDeactivate_(False)  # 失焦(点别处)时不自动隐藏,真正钉住
    try:
        panel.setCollectionBehavior_(1 << 0 | 1 << 8)  # CanJoinAllSpaces | FullScreenAuxiliary
    except Exception:
        pass

    # 毛玻璃背景
    eff = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
    eff.setMaterial_(NSVisualEffectMaterialHUDWindow)
    eff.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
    eff.setState_(NSVisualEffectStateActive)
    eff.setWantsLayer_(True)
    eff.layer().setCornerRadius_(RADIUS)
    eff.setAutoresizingMask_(18)  # width|height sizable
    panel.contentView().addSubview_(eff)

    view = HUDView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
    view.setAutoresizingMask_(18)
    eff.addSubview_(view)

    deleg = Delegate.alloc().init()
    deleg.view = view
    deleg.panel = panel
    deleg.minimized = False
    deleg.expandedH = H
    deleg.archived_view = False
    deleg.mode = 0  # 0 悬浮 / 1 终端 / 2 并存
    _HUD_DELEG[0] = deleg
    view.rebuild()
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        2.0, deleg, "refresh:", None, True)

    # 右上角 ✕ 关闭按钮
    btn = NSButton.alloc().initWithFrame_(NSMakeRect(W - 26, H - 26, 18, 18))
    btn.setTitle_("✕")
    btn.setBordered_(False)
    btn.setTarget_(deleg)
    btn.setAction_("quit:")
    btn.setAutoresizingMask_(1 << 0 | 1 << 3)  # min-x margin flexible | max-y (top) fixed
    eff.addSubview_(btn)

    # ✕ 左侧 –/+ 最小化/展开按钮
    mbtn = NSButton.alloc().initWithFrame_(NSMakeRect(W - 46, H - 26, 18, 18))
    mbtn.setTitle_("–")
    mbtn.setBordered_(False)
    mbtn.setTarget_(deleg)
    mbtn.setAction_("toggle:")
    mbtn.setAutoresizingMask_(1 << 0 | 1 << 3)
    eff.addSubview_(mbtn)
    deleg.minBtn = mbtn

    # 再往左:模式循环按钮(悬浮 / 终端 / 并存)
    cbtn = NSButton.alloc().initWithFrame_(NSMakeRect(W - 108, H - 27, 58, 20))
    cbtn.setTitle_("悬浮")
    cbtn.setBezelStyle_(1)  # rounded
    cbtn.setFont_(NSFont.systemFontOfSize_(10))
    cbtn.setTarget_(deleg)
    cbtn.setAction_("cycleMode:")
    cbtn.setAutoresizingMask_(1 << 0 | 1 << 3)
    eff.addSubview_(cbtn)

    # 再往左:归档区/看板 视图切换按钮
    abtn = NSButton.alloc().initWithFrame_(NSMakeRect(W - 170, H - 27, 58, 20))
    abtn.setTitle_("归档区")
    abtn.setBezelStyle_(1)  # rounded
    abtn.setFont_(NSFont.systemFontOfSize_(10))
    abtn.setTarget_(deleg)
    abtn.setAction_("toggleArchived:")
    abtn.setAutoresizingMask_(1 << 0 | 1 << 3)
    eff.addSubview_(abtn)

    panel.makeKeyAndOrderFront_(None)
    panel.makeFirstResponder_(view)
    app.activateIgnoringOtherApps_(True)
    app.run()
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(prog="cw", description="Claude 多终端调度器")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("up", help="起/连 tmux 会话并绑定看板热键")
    p_board = sub.add_parser("board", help="运行看板 TUI")
    p_board.add_argument("--demo", action="store_true", help="用样例数据预览(不连真实会话)")
    sub.add_parser("status", help="打印会话/作业 JSON")
    p_list = sub.add_parser("list", help="打印看板卡片(纯文本)")
    p_list.add_argument("--demo", action="store_true", help="用样例数据")
    p_list.add_argument("--by", choices=["project", "status"], default="project",
                        help="按项目(默认)或状态分组")
    p_launch = sub.add_parser("launch", help="新建 Claude 窗口")
    p_launch.add_argument("cwd")
    p_launch.add_argument("prompt", nargs="?", default=None)
    p_launch.add_argument("--config", default=None,
                          help="配置标签 default/doubao/official(默认 default)")
    p_imp = sub.add_parser("import", help="用 --resume 导入现有会话")
    p_imp.add_argument("sid")
    p_pane = sub.add_parser("pane", help="聚焦/召出当前窗口的窗格(board|claude|services)")
    p_pane.add_argument("which", choices=["board", "claude", "services"])
    sub.add_parser("hud", help="macOS 原生悬浮看板(置顶浮窗,需 PyObjC)")
    sub.add_parser("services", help="运行服务面板 TUI(右侧窄栏)")
    args = ap.parse_args()
    cmd = args.cmd or "up"
    if cmd == "up":
        return cmd_up()
    if cmd == "board":
        return cmd_board(args.demo)
    if cmd == "status":
        return cmd_status()
    if cmd == "list":
        return cmd_list(args.demo, args.by)
    if cmd == "launch":
        return cmd_launch(args.cwd, args.prompt, args.config)
    if cmd == "import":
        return cmd_import(args.sid)
    if cmd == "pane":
        return cmd_pane(args.which)
    if cmd == "hud":
        return cmd_hud()
    if cmd == "services":
        return cmd_services()
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
