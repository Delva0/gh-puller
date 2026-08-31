"""Agent 监控 Web/WS hub(FastAPI 端点层)。

生产端是 gh_puller.agent 的 WsSink(经 AGENT_MONITOR_WEBUI_URL 接入,主动向 /ws 推
{"type":"evts","events":[...]});浏览器查看端经同一端点订阅会话。GET / 与 /viewer
直接出构建好的单文件 viewer(agent_monitor_viewer.html);hub 只持内存状态
(每会话非流式历史按 seq 索引,raw chunk 只实时转发;完备真源是 FileSink 磁盘 JSONL —— 启动种子加载后,
历史查看不再为空),写盘是 FileSink 的事,重启 hub 列表与历史均在。

磁盘布局扁平(<uuid>.jsonl,根 = AGENT_MONITOR_DIR,见 gh_puller.agent.sinks.FileSink):
分类学隐式化 —— 会话键 = 文件内 session/start 的 session 字段(<ns>/<uuid4>),
状态 = 有无 session/end(有:按 data.state 分 completed/aborted;无:running;
超租约(AGENT_MONITOR_LEASE_SECS)派生 aborted 的判态依据见
gh_puller/agent/events.py 会话保活 —— 只内存态不写盘,文件复活自愈回 running)。
index 时对 running 会话按需重判(文件 mtime 变化才重读尾部找终态);
租约扫描(scan + lifespan 循环)周期把过期 running 翻转为 aborted 并向在线查看端广播。

协议:一连接一角色,首帧定角色(evts/evt → 生产端,其余 → 查看端);查看端帧:
- index → {type:"index", sessions:[{session, run_id, label, provider, model, state,
  ts, last_ts, num_events}]}(last_ts 降序);
- history {session, beforeSeq?, max?} → {type:"history", session, events, hasMore,
  nextBeforeSeq}:磁盘+内存合并的 seq 升序页(beforeSeq 缺省读尾部;nextBeforeSeq
  为 oldest in-page,客户端以此翻旧页),只含非流式投影。文件 seq 允许洞(洞=被跳过的
  assistant/chunk),客户端只按 seq 排序/比较,不作稠密假设;
- subscribe {session} → {type:"evt_ready", session, lastSeq} 后实时
  {type:"evts","events":[...]} 批量推送(单订阅视图:一连接只盯一会话;
  兼容旧生产端的单事件帧);
- delete {session} → 内存移除 + 磁盘 JSONL 删除,随后向全部查看端广播
  新 {type:"index"}(复用既有帧型,请求端在其中;幽灵删除同样广播,客户端
  以索引回响推进列表,无需 ack 帧);
- ping → pong。
live 帧与生产端同构(带 seq),客户端按 seq 去重/接缝。
"""

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from gh_puller import envs
from gh_puller.agent.events import NON_STREAM_TYPES

# 租约缺省(仅 hub 消费);LEASE 须 ≥ 3~5×HEARTBEAT ——
# AGENT_MONITOR_HEARTBEAT_SECS 在 gh_puller.envs(生产端 sinks 消费,包内契约)。
_DEFAULT_LEASE_SECS = int(os.environ.get("AGENT_MONITOR_LEASE_SECS", "150"))


def _log(msg: str) -> None:
    print(f"[agent-monitor] {msg}", file=sys.stderr, flush=True)


def _file_stem(session: str) -> str:
    """会话 id → 文件名 stem(uuid 段):取最后一个 "/" 后段(与 FileSink 同映射)。

    session id 形如 <ns>/<uuid4>(ns 由上层业务定);显式无斜杠 session 原样。
    """
    return session.rsplit("/", 1)[-1]


class _Session:
    """hub 内存中的单会话状态:非流式历史索引与原始流高水位。"""

    def __init__(self, session: str, label: str = "", provider: str = "", model: str = "",
                 run_id: str | None = None, generator: str = ""):
        self.session = session
        self.label = label or session
        self.provider = provider
        self.generator = generator
        self.model = model
        self.run_id = run_id
        self.state = "running"
        self.ts = time.time()
        self.last_ts = self.ts
        self.events: dict[int, dict] = {}  # seq → 非流式历史事件(feed 实时写入;种子时载入)
        self.last_seq: int | None = None  # 原始事件流高水位(chunk 不进入 events)
        self.subscribers: set = set()  # live evt 订阅(查看端;单视图语义)
        self.disk_mtime: float = 0.0  # 最近一次重判时文件的 mtime(0 = 未查)
        self.history_mtime: float = 0.0  # 最近一次历史缓存同步时的文件 mtime


class _Hub:
    """hub 内存状态:磁盘种子 + live 事件接收 + 按会话订阅广播。"""

    def __init__(self, lease_secs: float | None = None):
        self.sessions: dict[str, _Session] = {}
        self.viewers: set = set()  # 所有查看端连接(新会话/终态时广播 index;不同于 per-session subscribers)
        self.root: str | None = None  # AGENT_MONITOR_DIR(history 合并磁盘用)
        self.lease_secs = _DEFAULT_LEASE_SECS if lease_secs is None else lease_secs

    def _file_for(self, session: str) -> Path | None:
        """会话 → 磁盘文件路径(扁平布局);root 未设 → None。"""
        if not self.root:
            return None
        return Path(self.root) / f"{_file_stem(session)}.jsonl"

    def seed(self, root: str | None) -> None:
        """启动种子:扫描根目录下的 *.jsonl 扁平文件,载入事件溯源格式。

        会话键取文件内 session/start 的 session 字段(文件名只是 stem,无状态目录);
        状态隐式判定:文件含 session/end → 按 data.state 分 completed/aborted,
        无 → running(崩溃残留/运行中)。旧(聚合 LLM 行)格式 / 坏行不可折叠 →
        跳过并日志(不影响其余会话);live 续写经 feed(同 seq 覆盖,天然去重)。
        """
        if not root:
            return
        self.root = root
        base = Path(root)
        for path in sorted(base.glob("*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception as exc:
                _log(f"hub 种子跳过 {path.name}: 读取失败 {exc}")
                continue
            events: dict[int, dict] = {}
            last_seq: int | None = None
            head: dict | None = None
            end_state: str | None = None
            for line in lines:
                if not line.strip():
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    _log(f"hub 种子跳过半行 {path.name}: 不可解析")
                    continue
                if head is None and evt.get("type") == "session/start":
                    head = evt
                if evt.get("type") == "session/end":
                    end_state = (evt.get("data") or {}).get("state", "completed")
                if "seq" not in evt or "type" not in evt:
                    continue  # 旧 v1 聚合行:无 seq/type,整体不可折叠
                seq = int(evt["seq"])
                last_seq = seq if last_seq is None else max(last_seq, seq)
                if evt.get("type") in NON_STREAM_TYPES:
                    events[seq] = evt
            if head is None or not events:
                _log(f"hub 种子跳过 {path.name}: 非事件溯源格式")
                continue
            d = head.get("data") or {}
            sid = head.get("session") or path.stem  # key = 事件内 session(文件名只是 stem)
            sess = _Session(sid, head.get("label") or d.get("label"),
                            head.get("provider") or d.get("provider"),
                            head.get("model") or d.get("model"),
                            run_id=head.get("run_id") or d.get("run_id"),
                            generator=head.get("generator") or d.get("generator") or "")
            sess.state = end_state or "running"
            sess.ts = head.get("ts") or sess.ts
            sess.last_ts = max((e.get("ts") or sess.ts) for e in events.values())
            sess.events = events
            sess.last_seq = last_seq
            try:
                sess.disk_mtime = path.stat().st_mtime
                sess.history_mtime = sess.disk_mtime
            except OSError:
                sess.disk_mtime = 0.0
            if (end_state is None and sess.disk_mtime
                    and time.time() - sess.disk_mtime > self.lease_secs):
                sess.state = "aborted"  # 崩溃残留:文件僵死超租约 → 孤儿(纯派生,不写盘;复活自愈见重判)
            self.sessions[sid] = sess

    def _recheck_state(self, sess: _Session) -> None:
        """按需重判(隐式分类学):running/aborted 会话的磁盘文件 mtime 变化 → 重读尾部找终态。

        session/end 必为文件最后一条合法行(适配器 finish 的 finally 兜底);
        文件尾部 64KB 内反向扫描即可。mtime 不变 → 不动(stat 与读盘分离,
        全量跳过的会话零读盘)。自愈场景:seed 后文件继续被写(WS 漏帧/断接),
        或崩溃残留文件被外部补写终态;孤儿(租约派生 aborted)文件复活 →
        尾部无终态行 → 置回 running(生产端仍活着)。
        """
        if sess.state == "completed":
            return
        path = self._file_for(sess.session)
        if path is None or not path.exists():
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime == sess.disk_mtime:
            return
        was_aborted = sess.state == "aborted"
        sess.disk_mtime = mtime
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            return
        for line in reversed(tail.splitlines()):
            if not line.strip():
                continue
            try:
                evt = json.loads(line)
            except Exception:  # noqa: S112 - 半行/坏行静默跳过(协议层容错,继续看更早的行)
                continue
            if evt.get("type") == "session/end":
                sess.state = (evt.get("data") or {}).get("state", "completed")
                sess.last_ts = max(sess.last_ts, evt.get("ts") or 0)
            elif was_aborted:
                sess.state = "running"  # 复活自愈:文件前进但无终态行 → 生产端仍活着
            break  # 只判最后一条合法文件行

    async def scan(self) -> None:
        """租约扫描:无终态行且文件 mtime 静止超租约 → 派生 aborted(内存态,不写盘)。

        先按 _recheck_state 语义重判(mtime 变化 → 终态 / 复活自愈),再对仍 running
        的会话套租约;任一会话真实翻转 → 广播一次 index(幂等:无翻转零广播)。
        无 root / 文件缺失(delete 竞态)的会话跳过 —— 完成态不检查。
        """
        flipped = False
        for sess in self.sessions.values():
            if sess.state == "completed":
                continue
            self._recheck_state(sess)
            if sess.state != "running":
                continue
            path = self._file_for(sess.session)
            if path is None or not path.exists():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if time.time() - mtime > self.lease_secs:
                sess.state = "aborted"
                flipped = True
                _log(f"hub 租约:会话 {sess.session} 无终态行且文件静止超过 "
                     f"{self.lease_secs:g}s → 孤儿(aborted)")
        if flipped:
            await self._broadcast_index()

    def index(self) -> list[dict]:
        """会话索引(最近更新在前):供左列表渲染;先对 running 会话按需重判。"""
        for sess in self.sessions.values():
            self._recheck_state(sess)
        return sorted(
            (
                {"session": s.session, "run_id": s.run_id, "label": s.label,
                 "generator": s.generator, "provider": s.provider, "model": s.model,
                 "state": s.state, "ts": s.ts, "last_ts": s.last_ts,
                 "num_events": (s.last_seq + 1) if s.last_seq is not None else 0}
                for s in self.sessions.values()
            ),
            key=lambda x: x["last_ts"],
            reverse=True,
        )

    async def delete(self, session: str) -> None:
        """查看端删除会话:内存移除 + 磁盘文件删除 + 广播新索引。

        磁盘与内存一体删(完备真源在磁盘;只删内存则重启后复活)。
        运行中会话被删:生产端下一事件经 feed 惰性重建,列表与历史从头再来。
        """
        self.sessions.pop(session, None)
        path = self._file_for(session)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                _log(f"hub 删除会话磁盘文件失败 {path.name}: {exc}")
        await self._broadcast_index()

    async def _push(self, subs: set, payload: str) -> None:
        """向订阅集合逐端发一帧;发送失败记日志并剔除该端(不中断其余端)。"""
        for sub in list(subs):
            try:
                await sub.send_text(payload)
            except Exception as exc:
                _log(f"hub 订阅端断连,移除订阅: {type(exc).__name__}: {exc}")
                subs.discard(sub)

    async def _broadcast_index(self) -> None:
        """推送完整会话索引到所有查看端(新会话/终态变更时;协议不新增帧型)。"""
        await self._push(self.viewers, json.dumps(
            {"type": "index", "sessions": self.index()}, ensure_ascii=False))

    def _session_events(self, sess: _Session) -> dict[int, dict]:
        """同步磁盘的非流式投影并返回缓存(seq 键;live 内存优先)。

        文件为扁平布局(<uuid>.jsonl);seq 允许洞(洞=被跳过的
        assistant/chunk),按键合并天然兼容。
        """
        cand = self._file_for(sess.session)
        if cand is not None and cand.exists():
            try:
                mtime = cand.stat().st_mtime
            except OSError:
                return sess.events
            if mtime == sess.history_mtime:
                return sess.events
            disk: dict[int, dict] = {}
            for line in cand.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    evt = json.loads(line)
                except Exception:  # noqa: S112 - 坏行静默跳过(旧文件尾部半行/截断行,协议层容错)
                    continue
                if "seq" not in evt:
                    continue  # 旧格式行跳过
                if evt.get("type") in NON_STREAM_TYPES:
                    disk[int(evt["seq"])] = evt
            disk.update(sess.events)
            sess.events = disk
            sess.history_mtime = mtime
        return sess.events

    def _history_page(self, sess: _Session, before: int | None, max_n: int):
        """seq 升序分页:取 beforeSeq(缺省无限)之下最近 max_n 条;翻旧页用 nextBeforeSeq。"""
        merged = self._session_events(sess)
        seqs = sorted(k for k in merged if before is None or k < before)
        page = seqs[-max_n:]
        has_more = len(seqs) > max_n
        next_before = page[0] if (has_more and page) else None
        return [merged[k] for k in page], has_more, next_before

    def _record(self, evt: dict) -> tuple[_Session | None, bool]:
        """Record one event and return its session plus whether index metadata changed."""
        sid = evt.get("session") or ""
        if not sid:
            return None, False
        created = self.sessions.get(sid) is None
        sess = self.sessions.get(sid)
        if sess is None:
            d = evt.get("data") or {}
            sess = _Session(sid, evt.get("label") or d.get("label"),
                            evt.get("provider") or d.get("provider"),
                            evt.get("model") or d.get("model"),
                            run_id=evt.get("run_id") or d.get("run_id") or None,
                            generator=evt.get("generator") or d.get("generator") or "")
            self.sessions[sid] = sess
        seq = evt.get("seq")
        if seq is not None:
            seq = int(seq)
            sess.last_seq = seq if sess.last_seq is None else max(sess.last_seq, seq)
            if evt.get("type") in NON_STREAM_TYPES:
                sess.events[seq] = evt
        sess.last_ts = evt.get("ts") or time.time()
        if evt.get("type") == "session/start":
            d = evt.get("data") or {}
            sess.label = evt.get("label") or d.get("label") or sess.label
            sess.provider = evt.get("provider") or d.get("provider") or sess.provider
            sess.generator = evt.get("generator") or d.get("generator") or sess.generator
            sess.model = evt.get("model") or d.get("model") or sess.model
            sess.run_id = evt.get("run_id") or d.get("run_id") or sess.run_id
        elif evt.get("type") == "session/end":
            sess.state = (evt.get("data") or {}).get("state", "completed")
        return sess, created or evt.get("type") == "session/end"

    async def feed_batch(self, events: list[dict]) -> None:
        """Record a producer batch and forward one batch per subscribed session."""
        live: dict[str, list[dict]] = {}
        index_changed = False
        for evt in events:
            sess, changed = self._record(evt)
            if sess is None:
                continue
            index_changed = index_changed or changed
            if sess.subscribers:
                live.setdefault(sess.session, []).append(evt)
        for sid, batch in live.items():
            await self._push(self.sessions[sid].subscribers, json.dumps(
                {"type": "evts", "events": batch}, ensure_ascii=False))
        if index_changed:
            await self._broadcast_index()

    async def feed(self, evt: dict) -> None:
        """Backward-compatible single-event producer entry."""
        sess, index_changed = self._record(evt)
        if sess is None:
            return
        if sess.subscribers:
            await self._push(sess.subscribers, json.dumps(
                {"type": "evt", "event": evt}, ensure_ascii=False))
        if index_changed:
            await self._broadcast_index()


def _viewer_html(static_root: Path) -> bytes:
    """读静态产物(static/agent_monitor_viewer.html);缺件回退文案。"""
    path = static_root / "agent_monitor_viewer.html"
    if path.exists():
        return path.read_bytes()
    _log(f"viewer 未构建:请运行仓库根 pnpm build(期望 {path})")
    return "viewer 文件缺失".encode()


async def _producer_loop(ws: WebSocket, hub: _Hub, first: dict) -> None:
    """生产端(gh_puller WsSink):feed 事件/应答 ping。"""
    if first.get("type") == "evts":
        await hub.feed_batch(first.get("events") or [])
    else:
        await hub.feed(first.get("event") or {})
    try:
        while True:
            frame = json.loads(await ws.receive_text())
            data = frame.get("type")
            if data == "evt":
                await hub.feed(frame.get("event") or {})
            elif data == "evts":
                await hub.feed_batch(frame.get("events") or [])
            elif data == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _log(f"hub 生产端帧处理失败: {type(exc).__name__}: {exc}")


async def _viewer_frame(ws: WebSocket, hub: _Hub, frame: dict) -> None:
    kind = frame.get("type")
    if kind == "index":
        await ws.send_text(json.dumps({"type": "index", "sessions": hub.index()}, ensure_ascii=False))
    elif kind == "history":
        sid = frame.get("session") or ""
        sess = hub.sessions.get(sid)
        if sess is None:
            await ws.send_text(json.dumps(
                {"type": "history", "session": sid, "events": [], "hasMore": False,
                 "nextBeforeSeq": None}, ensure_ascii=False))
            return
        events, has_more, next_before = hub._history_page(
            sess, frame.get("beforeSeq"), int(frame.get("max") or 200))
        await ws.send_text(json.dumps(
            {"type": "history", "session": sid, "events": events, "hasMore": has_more,
             "nextBeforeSeq": next_before}, ensure_ascii=False))
    elif kind == "subscribe":
        sid = frame.get("session") or ""
        sess = hub.sessions.get(sid)
        for s in hub.sessions.values():
            s.subscribers.discard(ws)  # 单视图:一条连接只盯一个会话
        last_seq = None
        if sess is not None:
            sess.subscribers.add(ws)  # 先登记再应答:live 推送无缝隙
            last_seq = sess.last_seq
        await ws.send_text(json.dumps(
            {"type": "evt_ready", "session": sid, "lastSeq": last_seq}, ensure_ascii=False))
    elif kind == "delete":
        await hub.delete(frame.get("session") or "")
    elif kind == "ping":
        await ws.send_text(json.dumps({"type": "pong"}))


async def _viewer_loop(ws: WebSocket, hub: _Hub, first: dict) -> None:
    """查看端(浏览器/回环测试):页面帧应答;断连清理订阅。"""
    try:
        await _viewer_frame(ws, hub, first)
        while True:
            await _viewer_frame(ws, hub, json.loads(await ws.receive_text()))
    except WebSocketDisconnect:
        pass
    finally:
        hub.viewers.discard(ws)
        for sess in hub.sessions.values():
            sess.subscribers.discard(ws)


async def _lease_loop(h: _Hub) -> None:
    """租约扫描循环:翻转孤儿会话并广播;异常只记日志不退出(监控不拖垮主服务)。"""
    while True:
        try:
            await h.scan()
        except Exception as exc:
            _log(f"hub 租约扫描异常: {exc}")
        await asyncio.sleep(max(1.0, h.lease_secs / 4))


def create_app(hub: _Hub | None = None, *, static_root: Path | None = None) -> FastAPI:
    """组装 FastAPI 应用(测试可注入空 hub / 静态目录);缺省 hub 启动时磁盘种子。"""
    h = hub if hub is not None else _Hub()
    if hub is None:
        h.seed(envs.AGENT_MONITOR_DIR)
    root = static_root if static_root is not None else Path(__file__).parent / "static"

    @asynccontextmanager
    async def _lifespan(_app):
        scan_task = asyncio.create_task(_lease_loop(h))
        try:
            yield
        finally:
            scan_task.cancel()
            await asyncio.gather(scan_task, return_exceptions=True)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)

    @app.get("/", include_in_schema=False)
    @app.get("/viewer", include_in_schema=False)
    async def index() -> Response:
        return Response(_viewer_html(root), media_type="text/html; charset=utf-8")

    @app.websocket("/ws")
    async def ws(ws: WebSocket) -> None:
        await ws.accept()
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
        except Exception:  # 超时/断开/非 JSON:静默释放
            return
        if first.get("type") in {"evt", "evts"}:
            await _producer_loop(ws, h, first)
        else:  # index/history/subscribe/ping:一律按查看端处理
            h.viewers.add(ws)  # 广播 index 的受众(离开时 _viewer_loop finally 剔除)
            await _viewer_loop(ws, h, first)

    return app


app = create_app()
