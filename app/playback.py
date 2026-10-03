"""播放会话：单进程内存态，让已保存场景在播放中接纳后续修订。

会话在创建时复制场景当时的完整快照（字幕、分轨、changes、源版本），
游标从 0 毫秒开始，之后支持两种操作：

- 推进（advance）：提交非负整数 time_ms，游标只能不减；返回源版本、
  游标，以及该时刻处于活动区间 [start_ms, end_ms)（左闭右开）的字幕
  及其轨道。
- 事件推进（advance_events）：同样提交非负整数 time_ms 推进游标，
  额外返回“旧游标 < 事件时刻 <= time_ms”窗口内的全部进入（enter）/
  退出（exit）事件——一次推进跨过整条短字幕也不会漏掉。事件按时刻
  升序，同刻先退出后进入，同类按会话字幕顺序。0 毫秒已活动的字幕由
  创建响应的快照确定，不产生进入事件。事件只随本次推进生成，不保存
  历史、不提供重放。
- 临时隐藏说话人（set_hidden_speakers）：按 speaker 精确匹配隐藏字幕，
  仅作用于本会话的内存状态，默认空、不持久化。设置时游标不变，以当前
  游标的左闭右开活动区间判断显示差异：原可见而现隐藏的活动字幕发
  exit，原隐藏而现可见者发 enter；事件沿用原事件字段、时刻取当前游标，
  先 exit 后 enter，同类按会话字幕顺序，集合无变化则无事件。设置后，
  推进与事件推进的 active、events 均排除隐藏者，但游标照常推进；接纳
  不改变隐藏集合。完整快照（state()/finish）与分轨不受影响。
- 接纳（accept）：把场景当前快照并入会话。轨道数不得变；游标前已开始
  （start_ms <= 游标）的字幕，其 id、speaker、起止时间、locked_track
  必须不变，并固定原分轨；其余字幕可改、可删、可新增，但都必须在游标
  之后开始。随后按新快照顺序，以最少换轨与并列规则重算全部分轨。
  冲突或无解即失败且会话保持不变；成功则整体替换会话快照与源版本，
  场景本身不受影响。
- 结束（finish）：一次完整操作取回会话最终完整快照并把会话从注册表
  移除。结束与同一会话的推进、事件推进、隐藏切换、接纳在会话锁内串行
  生效；快照只取自轮到结束生效时的会话内存状态，不读取场景、因此不会
  纳入尚未接纳的场景修订。成功响应沿用创建会话的全部字段。结束后该会话
  ID 的一切请求（含已取得会话引用、在锁上等待的在途请求）均返回 404，
  并发结束最多一次成功。结束不改动源场景、其他会话或此前返回的快照，
  不保存结束记录，也不跨重启恢复。

会话只存在于本进程内存中，不随重启保留。同一会话上的推进、事件推进、
隐藏切换、接纳与结束通过会话级互斥锁串行化：每次调用要么完整生效，
要么完全无效。
接纳是一个完整操作——场景快照的读取（load_record 回调）、已开始字幕
校验、分轨重算与会话替换都在会话锁内完成，因此并发下迟到的接纳请求
读到的一定是轮到它生效时的场景快照，不会把会话回写到进入锁之前读到的
旧版本。结束在会话锁内置位 closed 墓碑并移除注册表条目：先取得会话
引用、在锁上排队的请求轮到它生效时会看到墓碑而失败（404），不会在
结束之后再对已结束的会话生效一次。
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .solver import Subtitle, solve
from .storage import SceneRecord


class AcceptConflict(Exception):
    """接纳场景当前快照失败（对应 HTTP 409）。

    原因包括：轨道数变化、已开始字幕被修改/删除、新增或修改的字幕
    未在游标之后开始、固定已开始字幕的原分轨后重算无解。
    """


class CursorRegression(Exception):
    """推进时间小于当前游标（对应 HTTP 422）。"""

    def __init__(self, cursor_ms: int, time_ms: int) -> None:
        self.cursor_ms = cursor_ms
        self.time_ms = time_ms
        super().__init__(
            f"time_ms={time_ms} 小于当前游标 cursor_ms={cursor_ms}，游标只能不减"
        )


class SceneUnavailable(Exception):
    """接纳生效时场景已不存在（对应 HTTP 404）。"""

    def __init__(self, scene_id: str) -> None:
        self.scene_id = scene_id
        super().__init__(f"场景 {scene_id!r} 不存在")


class SessionMissing(Exception):
    """注册表中不存在该会话 id（对应 HTTP 404）。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"播放会话 {session_id!r} 不存在")


class SessionFinished(Exception):
    """会话已结束：请求已取得引用但轮到它生效时墓碑已置位（对应 HTTP 404）。

    结束是完整操作：在会话锁内把 closed 置为 True 并移除注册表条目。
    此前已取得 PlaybackSession 引用、正在会话锁上排队的推进/事件推进/
    接纳/结束请求，轮到它生效时看到 closed 即按未知会话处理（404），
    不会在结束之后再对该会话生效一次。
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"播放会话 {session_id!r} 已结束")


@dataclass
class PlaybackSession:
    """一次播放会话的内存状态；subtitles 与 tracks 按下标对齐。"""

    session_id: str
    scene_id: str
    source_revision: int
    track_count: int
    subtitles: List[Dict[str, Any]]
    tracks: List[int]
    changes: int
    cursor_ms: int = 0
    # 临时隐藏的说话人集合：按 speaker 精确匹配，仅影响本会话的
    # active/events 视图；默认空，接纳后保留，不随会话持久化。
    hidden_speakers: set = field(default_factory=set)
    # 结束墓碑：finish 在会话锁内置为 True 并移除注册表条目。此后即使
    # 仍持有本对象引用的在途请求，在锁内读到 closed 也一律按 404 失败。
    closed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def active(self) -> List[Dict[str, Any]]:
        """游标时刻处于 [start_ms, end_ms) 内且未隐藏的字幕及轨道。

        隐藏只过滤播放视图：区间判断仍为左闭右开，分轨与完整快照不变。
        """
        hidden = self.hidden_speakers
        return [
            {**sub, "track": track}
            for sub, track in zip(self.subtitles, self.tracks)
            if sub["speaker"] not in hidden
            and sub["start_ms"] <= self.cursor_ms < sub["end_ms"]
        ]

    def state(self) -> Dict[str, Any]:
        """会话完整快照（纯数据，可直接作为响应体）。"""
        return {
            "session_id": self.session_id,
            "scene_id": self.scene_id,
            "source_revision": self.source_revision,
            "cursor_ms": self.cursor_ms,
            "track_count": self.track_count,
            "changes": self.changes,
            "subtitles": [dict(s) for s in self.subtitles],
            "assignments": [
                {"id": s["id"], "track": t}
                for s, t in zip(self.subtitles, self.tracks)
            ],
        }


def _identity(sub: Dict[str, Any]) -> Tuple[Any, ...]:
    """已开始字幕在接纳时必须保持不变的字段组合。"""
    return (
        sub["id"],
        sub["speaker"],
        sub["start_ms"],
        sub["end_ms"],
        sub.get("locked_track"),
    )


class PlaybackManager:
    """会话注册表：单进程内存实现，不持久化、不跨重启保留。

    锁序约定为“会话锁 -> 注册表锁”：finish 先在注册表中查到会话，再
    进入会话锁，最后在会话锁内取注册表锁移除条目。其余路径从不持有
    注册表锁等待会话锁，因此不存在反向等待、不会死锁。
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, PlaybackSession] = {}
        self._registry_lock = threading.Lock()

    def create(self, record: SceneRecord) -> Dict[str, Any]:
        """按场景当前快照创建会话：复制完整快照，游标从 0 毫秒开始。"""
        subtitles = [dict(s) for s in record.subtitles]
        track_by_id = {a["id"]: a["track"] for a in record.assignments}
        session = PlaybackSession(
            session_id=uuid.uuid4().hex,
            scene_id=record.scene_id,
            source_revision=record.revision,
            track_count=record.track_count,
            subtitles=subtitles,
            tracks=[track_by_id[s["id"]] for s in subtitles],
            changes=record.changes,
        )
        state = session.state()
        with self._registry_lock:
            self._sessions[session.session_id] = session
        return state

    def get(self, session_id: str) -> Optional[PlaybackSession]:
        with self._registry_lock:
            return self._sessions.get(session_id)

    def advance(self, session: PlaybackSession, time_ms: int) -> Dict[str, Any]:
        """推进游标（只能不减），返回源版本、游标与活动字幕及轨道。"""
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            if time_ms < session.cursor_ms:
                raise CursorRegression(session.cursor_ms, time_ms)
            session.cursor_ms = time_ms
            return {
                "session_id": session.session_id,
                "scene_id": session.scene_id,
                "source_revision": session.source_revision,
                "cursor_ms": session.cursor_ms,
                "active": session.active(),
            }

    def advance_events(
        self, session: PlaybackSession, time_ms: int
    ) -> Dict[str, Any]:
        """推进游标并返回本次跨过的全部进入/退出事件。

        事件窗口为（旧游标, time_ms]：旧游标 < 事件时刻 <= time_ms。
        字幕在 start_ms 进入、end_ms 退出（区间左闭右开）；因此一次
        采样间隔内开始又结束的短字幕会同时给出退出与进入事件，不会漏。
        0 毫秒已活动（start_ms == 0）的字幕由创建会话时的快照确定，
        初始游标即 0，不产生进入事件。事件按时刻升序；同刻先退出后
        进入；同类按会话字幕顺序。time_ms 等于当前游标时事件为空。

        事件生成、游标更新与活动字幕计算基于同一份会话快照，在会话锁
        内一次完成，与推进、接纳、结束完整串行。倒退抛 CursorRegression，
        会话保持不变。事件不保存历史、不提供重放。隐藏说话人的字幕不
        进入 active，也不产生事件；游标仍照常推进。
        """
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            if time_ms < session.cursor_ms:
                raise CursorRegression(session.cursor_ms, time_ms)
            old_cursor = session.cursor_ms
            # 每项为 (时刻, 同刻次序, 会话顺序, 事件体)；同刻次序令 exit 在前
            pending: List[Tuple[int, int, int, Dict[str, Any]]] = []
            hidden = session.hidden_speakers
            for order, (sub, track) in enumerate(
                zip(session.subtitles, session.tracks)
            ):
                # 隐藏说话人的字幕既不进入 active，也不产生进入/退出事件
                if sub["speaker"] in hidden:
                    continue
                start_ms = sub["start_ms"]
                end_ms = sub["end_ms"]
                base = {
                    "id": sub["id"],
                    "speaker": sub["speaker"],
                    "track": track,
                }
                if old_cursor < start_ms <= time_ms:
                    pending.append(
                        (start_ms, 1, order, {**base, "time_ms": start_ms, "type": "enter"})
                    )
                if old_cursor < end_ms <= time_ms:
                    pending.append(
                        (end_ms, 0, order, {**base, "time_ms": end_ms, "type": "exit"})
                    )
            pending.sort(key=lambda item: (item[0], item[1], item[2]))
            events = [item[3] for item in pending]
            session.cursor_ms = time_ms
            return {
                "session_id": session.session_id,
                "scene_id": session.scene_id,
                "source_revision": session.source_revision,
                "cursor_ms": session.cursor_ms,
                "events": events,
                "active": session.active(),
            }

    def set_hidden_speakers(
        self, session: PlaybackSession, hidden_speakers: List[str]
    ) -> Dict[str, Any]:
        """替换本会话临时隐藏的说话人集合，并返回当前游标下的显示变更。

        按 speaker 精确匹配；hidden_speakers 由调用方保证为无重复、非空
        字符串序列（非法集合在 HTTP 层按 422 拒绝，本方法不负责校验），
        允许包含会话字幕中尚未出现（甚至永不出现）的说话人。游标不移动：
        以当前游标的左闭右开活动区间 [start_ms, end_ms) 判断新旧可见性——

        - 旧集合可见、新集合隐藏且此刻活动的字幕，产生 exit；
        - 旧集合隐藏、新集合可见且此刻活动的字幕，产生 enter；
        - 事件沿用事件推进的字段（id/speaker/track/time_ms/type），
          time_ms 取当前游标；先全部 exit 后全部 enter，同类按会话字幕
          顺序；新旧集合下显示状态相同的字幕不产生事件，集合无变化则
          事件为空。

        设置成功后，推进与事件推进的 active、events 都排除隐藏者，但
        游标照常推进；完整快照（state()/finish）、分轨与冻结规则不变。
        设置仅存于会话内存、不持久化，接纳不改变隐藏集合。与推进、事件
        推进、接纳、结束在会话锁内完整串行；会话已结束抛 SessionFinished。
        """
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            old_hidden = session.hidden_speakers
            new_hidden = set(hidden_speakers)
            cursor = session.cursor_ms
            exits: List[Tuple[int, Dict[str, Any]]] = []
            enters: List[Tuple[int, Dict[str, Any]]] = []
            for order, (sub, track) in enumerate(
                zip(session.subtitles, session.tracks)
            ):
                speaker = sub["speaker"]
                was_hidden = speaker in old_hidden
                is_hidden = speaker in new_hidden
                if was_hidden == is_hidden:
                    continue
                # 仅对当前游标左闭右开区间内的活动字幕发显示变更事件
                if not (sub["start_ms"] <= cursor < sub["end_ms"]):
                    continue
                event = {
                    "id": sub["id"],
                    "speaker": speaker,
                    "track": track,
                    "time_ms": cursor,
                    "type": "exit" if is_hidden else "enter",
                }
                (exits if is_hidden else enters).append((order, event))
            exits.sort(key=lambda item: item[0])
            enters.sort(key=lambda item: item[0])
            # 先 exit 后 enter；同类已按会话字幕顺序
            events = [item[1] for item in exits] + [
                item[1] for item in enters
            ]
            session.hidden_speakers = new_hidden
            return {
                "session_id": session.session_id,
                "scene_id": session.scene_id,
                "source_revision": session.source_revision,
                "cursor_ms": cursor,
                # 回显本次接纳的集合：去重后保持提交顺序
                "hidden_speakers": list(hidden_speakers),
                "events": events,
                "active": session.active(),
            }

    def accept(
        self,
        session: PlaybackSession,
        load_record: Callable[[], Optional[SceneRecord]],
    ) -> Dict[str, Any]:
        """接纳场景当前快照：读取场景、校验、重算分轨、替换会话为完整操作。

        load_record 在会话锁内调用，因此读到的是“轮到本次接纳生效时”的
        场景快照；与同一会话的推进/接纳/结束串行。这样并发下迟到的请求
        不会把进入锁之前读到的旧快照回写到会话上。会话已结束抛
        SessionFinished（404）；场景不存在抛 SceneUnavailable；冲突或
        无解抛 AcceptConflict，会话保持不变。
        """
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            record = load_record()
            if record is None:
                raise SceneUnavailable(session.scene_id)
            if record.track_count != session.track_count:
                raise AcceptConflict(
                    f"轨道数不得变化：会话为 {session.track_count}，"
                    f"场景当前为 {record.track_count}"
                )

            # 游标前已开始（start_ms <= 游标）的字幕必须原样保留
            started = {
                s["id"]: s
                for s in session.subtitles
                if s["start_ms"] <= session.cursor_ms
            }
            new_by_id = {s["id"]: s for s in record.subtitles}
            for sid, old in started.items():
                new = new_by_id.get(sid)
                if new is None:
                    raise AcceptConflict(f"字幕 {sid!r} 已开始，不得删除")
                if _identity(new) != _identity(old):
                    raise AcceptConflict(
                        f"字幕 {sid!r} 已开始，其 id/speaker/起止时间/"
                        "locked_track 不得修改"
                    )
            # 其余字幕可改删或加入，但都必须在游标之后开始
            for new in record.subtitles:
                if new["start_ms"] <= session.cursor_ms and new["id"] not in started:
                    raise AcceptConflict(
                        f"字幕 {new['id']!r} 的开始时间不晚于游标 "
                        f"{session.cursor_ms}：新增/修改的字幕必须在游标之后开始"
                    )

            # 固定已开始字幕的原分轨，按新快照顺序与最少换轨、并列规则重算
            session_track = {
                s["id"]: t for s, t in zip(session.subtitles, session.tracks)
            }
            subs = [
                Subtitle(
                    id=s["id"],
                    speaker=s["speaker"],
                    start_ms=s["start_ms"],
                    end_ms=s["end_ms"],
                    locked_track=(
                        session_track[s["id"]]
                        if s["id"] in started
                        else s.get("locked_track")
                    ),
                )
                for s in record.subtitles
            ]
            tracks, changes = solve(subs, session.track_count)
            if tracks is None:
                raise AcceptConflict("固定已开始字幕的原分轨后，新快照无解")

            # 成功：整体替换会话快照与源版本（场景本身不变）。
            # hidden_speakers 是仅属于本会话的播放期设置，不在快照内，
            # 接纳后原样保留；active() 与事件推进自然按新字幕的 speaker 过滤。
            session.subtitles = [dict(s) for s in record.subtitles]
            session.tracks = list(tracks)
            session.changes = changes
            session.source_revision = record.revision
            return session.state()

    def finish(self, session_id: str) -> Dict[str, Any]:
        """结束会话：一次完整操作取回最终快照并移除注册表条目。

        完整操作的定序为：先在注册表中查到会话，再进入会话锁，在锁内
        置位 closed 墓碑并取出注册表条目，最后对轮到结束生效时的会话
        状态取快照。与同一会话的推进、事件推进、隐藏切换、接纳共用会话
        锁，因此与它们完整串行：

        - 快照只取自会话内存状态（session.state()），不读取场景存储，
          不会纳入尚未被接纳的场景修订；
        - 已取得会话引用但在锁上等待的在途请求，轮到它时看到 closed，
          一律抛 SessionFinished（404），不会在结束之后再生效一次；
        - 并发结束中第一个通过 closed 检查的请求成功，其余抛
          SessionFinished；注册表已无条目时抛 SessionMissing；
        - 不改动源场景、其他会话或此前返回的快照；不保存结束记录。

        返回的字典沿用创建会话（state()）的全部响应字段。
        """
        with self._registry_lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionMissing(session_id)
        with session.lock:
            if session.closed:
                raise SessionFinished(session_id)
            session.closed = True
            with self._registry_lock:
                # 注册表条目即本对象；以 closed 为准，防御性地按 id 移除
                self._sessions.pop(session_id, None)
            # state() 复制字幕与分轨：结束响应不受其后任何变化影响
            return session.state()
