"""补取通道测试：丢响应后补取未确认的字幕变更。

覆盖：
- 开启通道返回当前可见 active 与基线序号 0；重复开启 409；
- 普通推进、事件推进与隐藏切换产生的全部 enter/exit 事件入队，
  seq 从 1 连续编号；短字幕成对入队；接纳不重写已排队内容；
- 补取提交 ack_seq：非负整数（拒绝布尔值/字符串/浮点），只能确认
  此前补取已返回的最大 seq 以内的前缀（越界 422），旧确认不回退；
  返回确认位置与未确认事件（按 seq 递增，重复补取内容不变）；
- 开启、补取与既有会话操作串行；失败不改会话或通道；
- 结束会话清除通道；未知或已结束会话、未开启通道均 404；
- 旧接口（/advance、/events、/hidden_speakers）响应不变。
"""
import threading

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def scene_payload(track_count=2):
    """s1[0,1000) s2[800,3000)锁2 s3[1000,2500) —— 分轨 1/2/1，换轨 0。"""
    return {
        "track_count": track_count,
        "expected_revision": 0,
        "subtitles": [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
             "locked_track": 2},
            {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
        ],
    }


def revise(client, scene_id, expected_revision, subtitles, track_count=2):
    resp = client.put(f"/scenes/{scene_id}", json={
        "track_count": track_count,
        "expected_revision": expected_revision,
        "subtitles": subtitles,
    })
    assert resp.status_code == 200
    assert resp.json()["feasible"] is True
    return resp.json()


def make_session(client, scene_id="scene-chan"):
    resp = client.put(f"/scenes/{scene_id}", json=scene_payload())
    assert resp.status_code == 200
    resp = client.post("/sessions", json={"scene_id": scene_id})
    assert resp.status_code == 200
    return resp.json()


def open_channel(client, sid):
    return client.post(f"/sessions/{sid}/channel")


def ack(client, sid, ack_seq):
    return client.post(f"/sessions/{sid}/channel/ack", json={"ack_seq": ack_seq})


def _triples(events):
    return [(e["seq"], e["time_ms"], e["type"], e["id"]) for e in events]


# ---------------------------------------------------------------------------
# 开启通道
# ---------------------------------------------------------------------------


def test_open_channel_returns_visible_active_and_baseline_seq_zero():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    resp = open_channel(client, sid)
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sid
    assert body["scene_id"] == "scene-chan"
    assert body["source_revision"] == 1
    assert body["cursor_ms"] == 900
    assert body["acked_seq"] == 0
    # 当前可见 active：900 时 s1、s2 在屏
    assert [(a["id"], a["track"]) for a in body["active"]] == [
        ("s1", 1),
        ("s2", 2),
    ]


def test_open_channel_active_respects_hidden_speakers():
    """开启返回的 active 为当前可见活动字幕（排除隐藏者）。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    client.post(
        f"/sessions/{sid}/hidden_speakers", json={"hidden_speakers": ["Alice"]}
    )

    body = open_channel(client, sid).json()
    assert [a["id"] for a in body["active"]] == ["s2"]
    assert body["acked_seq"] == 0


def test_open_channel_twice_409_and_channel_unchanged():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    assert open_channel(client, sid).status_code == 200

    resp = open_channel(client, sid)
    assert resp.status_code == 409
    assert resp.json()["detail"]["session_id"] == sid

    # 失败的重复开启不改通道：事件仍从 seq 1 连续记录
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    body = ack(client, sid, 0).json()
    assert _triples(body["events"]) == [
        (1, 800, "enter", "s2"),
        (2, 1000, "exit", "s1"),
        (3, 1000, "enter", "s3"),
    ]


def test_open_channel_unknown_or_finished_session_404():
    client = TestClient(app)
    assert open_channel(client, "no-such-session").status_code == 404

    sid = make_session(client)["session_id"]
    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    assert open_channel(client, sid).status_code == 404


# ---------------------------------------------------------------------------
# 事件入队：普通推进 / 事件推进 / 隐藏切换
# ---------------------------------------------------------------------------


def test_plain_advance_enqueues_events_including_short_subtitles():
    """普通推进（/advance）跨过的短字幕也成对入队，响应本身不变。"""
    client = TestClient(app)
    client.put("/scenes/short", json={
        "track_count": 2,
        "expected_revision": 0,
        "subtitles": [
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1000},
            {"id": "b", "speaker": "B", "start_ms": 500, "end_ms": 600},
        ],
    })
    sid = client.post("/sessions", json={"scene_id": "short"}).json()["session_id"]
    assert open_channel(client, sid).status_code == 200

    # 普通推进一次跨过整条短字幕 b；响应不含 events（旧接口不变）
    resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": 1000})
    assert resp.status_code == 200
    assert "events" not in resp.json()

    # 补取：b 的进入与退出成对入队，a 的退出不漏；a 在 0 毫秒已活动，
    # 其进入由开启响应的 active 快照确定，事件流不补发
    body = ack(client, sid, 0).json()
    assert _triples(body["events"]) == [
        (1, 500, "enter", "b"),
        (2, 600, "exit", "b"),
        (3, 1000, "exit", "a"),
    ]
    assert body["events"][0]["track"] == 2
    assert body["events"][0]["speaker"] == "B"


def test_events_advance_enqueues_same_events_as_response():
    """事件推进入队的事件与其响应一致（仅多连续 seq）。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)

    resp = client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    pushed = resp.json()["events"]
    assert _triples([dict(e, seq=i + 1) for i, e in enumerate(pushed)]) == [
        (1, 800, "enter", "s2"),
        (2, 1000, "exit", "s1"),
        (3, 1000, "enter", "s3"),
    ]

    body = ack(client, sid, 0).json()
    # 内容与推进响应一致（仅多连续 seq）
    stripped = [
        {k: v for k, v in e.items() if k != "seq"} for e in body["events"]
    ]
    assert stripped == pushed
    assert _triples(body["events"]) == [
        (1, 800, "enter", "s2"),
        (2, 1000, "exit", "s1"),
        (3, 1000, "enter", "s3"),
    ]


def test_hidden_toggle_enqueues_events_and_seq_stays_contiguous():
    """隐藏切换事件入队，与推进事件共用连续序号。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    open_channel(client, sid)

    # 隐藏 Alice：s1 在游标 900 活动，发 exit（seq 1）
    client.post(
        f"/sessions/{sid}/hidden_speakers", json={"hidden_speakers": ["Alice"]}
    )
    # 恢复：s1 仍活动，发 enter（seq 2）
    client.post(
        f"/sessions/{sid}/hidden_speakers", json={"hidden_speakers": []}
    )
    # 推进到 1000：s1 退出、s3 进入（seq 3、4）
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})

    body = ack(client, sid, 0).json()
    assert _triples(body["events"]) == [
        (1, 900, "exit", "s1"),
        (2, 900, "enter", "s1"),
        (3, 1000, "exit", "s1"),
        (4, 1000, "enter", "s3"),
    ]


def test_events_before_open_are_not_recorded():
    """通道只记录开启之后的事件：开启前的推进不产生排队内容。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    open_channel(client, sid)

    body = ack(client, sid, 0).json()
    assert body["events"] == []
    assert body["acked_seq"] == 0


def test_accept_does_not_rewrite_queued_events():
    """接纳替换会话快照，但已排队事件保持入队时的内容与序号。"""
    client = TestClient(app)
    scene_id = "scene-chan"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    open_channel(client, sid)

    # 推进到 1000：s1 退出、s3 进入入队（seq 1、2）
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})

    # 修订：s3 已开始（游标 1000 时其 start_ms=1000）保持原样，
    # 新增未开始的 s4[3200,4000)；接纳成功
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
        {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000},
    ])
    assert client.post(f"/sessions/{sid}/accept").status_code == 200

    # 已排队事件不重写：内容与序号与入队时完全一致
    body = ack(client, sid, 0).json()
    assert _triples(body["events"]) == [
        (1, 1000, "exit", "s1"),
        (2, 1000, "enter", "s3"),
    ]

    # 接纳后的推进基于新快照入队：s3/s2 退出、新增的 s4 进入/退出，
    # seq 接续连续
    client.post(f"/sessions/{sid}/events", json={"time_ms": 4000})
    body = ack(client, sid, 2).json()
    assert _triples(body["events"]) == [
        (3, 2500, "exit", "s3"),
        (4, 3000, "exit", "s2"),
        (5, 3200, "enter", "s4"),
        (6, 4000, "exit", "s4"),
    ]


# ---------------------------------------------------------------------------
# 补取与确认
# ---------------------------------------------------------------------------


def test_ack_flow_confirm_prefix_and_remaining_events():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)

    # 未返回过任何事件：只能确认基线 0，ack_seq=1 越界 422
    resp = ack(client, sid, 1)
    assert resp.status_code == 422
    assert resp.json()["detail"]["returned_seq"] == 0

    # 推进产生 3 个事件（seq 1..3）
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})

    # 第一次补取：确认 0，返回全部未确认事件，按 seq 递增
    body = ack(client, sid, 0).json()
    assert body["acked_seq"] == 0
    assert [e["seq"] for e in body["events"]] == [1, 2, 3]

    # 确认到 2：剩余事件只剩 seq 3
    body = ack(client, sid, 2).json()
    assert body["acked_seq"] == 2
    assert _triples(body["events"]) == [(3, 1000, "enter", "s3")]

    # 越界（此前补取最多返回到 3）：422 且通道不变
    resp = ack(client, sid, 4)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["ack_seq"] == 4
    assert detail["returned_seq"] == 3

    # 确认到 3：无剩余事件
    body = ack(client, sid, 3).json()
    assert body["acked_seq"] == 3
    assert body["events"] == []

    # 新事件入队后继续补取：seq 连续（4、5）
    client.post(f"/sessions/{sid}/events", json={"time_ms": 2500})
    body = ack(client, sid, 3).json()
    assert body["acked_seq"] == 3
    assert _triples(body["events"]) == [(4, 2500, "exit", "s3")]
    body = ack(client, sid, 4).json()
    assert body["acked_seq"] == 4
    assert body["events"] == []


def test_old_ack_does_not_regress_confirmation():
    """旧确认不回退：ack_seq 小于当前确认位置时幂等成功。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})

    assert ack(client, sid, 0).json()["acked_seq"] == 0
    assert ack(client, sid, 3).json()["acked_seq"] == 3

    # 迟到的旧确认（ack_seq=1）：确认位置不回退，仍返回 seq>3 的事件
    body = ack(client, sid, 1).json()
    assert body["acked_seq"] == 3
    assert body["events"] == []

    # 再确认 0 同样幂等
    body = ack(client, sid, 0).json()
    assert body["acked_seq"] == 3


def test_redelivery_after_lost_response_keeps_content_and_seq():
    """补取响应丢失后重复补取：未确认事件的内容与序号不变。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})

    first = ack(client, sid, 0).json()
    # 模拟响应丢失：客户端未确认，用同一 ack_seq 重试补取
    second = ack(client, sid, 0).json()
    assert second == first
    assert _triples(second["events"]) == [
        (1, 800, "enter", "s2"),
        (2, 1000, "exit", "s1"),
        (3, 1000, "enter", "s3"),
    ]

    # 客户端收到后确认到 3，再补取为空
    assert ack(client, sid, 3).json()["events"] == []


def test_failed_ack_does_not_change_channel():
    """越界 422 不改通道：确认位置与已返回位置保持，可继续正常补取。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})

    assert ack(client, sid, 0).status_code == 200
    assert ack(client, sid, 99).status_code == 422

    # 通道未受影响：事件仍在，确认位置仍为 0
    body = ack(client, sid, 0).json()
    assert body["acked_seq"] == 0
    assert [e["seq"] for e in body["events"]] == [1, 2, 3]


@pytest.mark.parametrize(
    "ack_seq",
    [True, False, "1", "abc", 1.5, None, [1], {"seq": 1}],
)
def test_ack_seq_rejects_bool_string_and_non_integer(ack_seq):
    """ack_seq 须为非负整数：布尔值、字符串、浮点等一律 422。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    resp = client.post(f"/sessions/{sid}/channel/ack", json={"ack_seq": ack_seq})
    assert resp.status_code == 422


def test_ack_seq_negative_422():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    assert ack(client, sid, -1).status_code == 422


def test_ack_missing_field_422():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    resp = client.post(f"/sessions/{sid}/channel/ack", json={})
    assert resp.status_code == 422


def test_ack_without_channel_404():
    """会话存在但未开启通道：补取 404。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    resp = ack(client, sid, 0)
    assert resp.status_code == 404


def test_ack_unknown_or_finished_session_404():
    client = TestClient(app)
    assert ack(client, "no-such-session", 0).status_code == 404

    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    # 结束会话清除通道：开启与补取均 404
    assert ack(client, sid, 0).status_code == 404
    assert open_channel(client, sid).status_code == 404


# ---------------------------------------------------------------------------
# 旧接口响应不变 / 串行化
# ---------------------------------------------------------------------------


def test_existing_endpoints_responses_unchanged_with_channel_open():
    """通道开启后，旧接口的响应字段与内容保持不变。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)

    # /advance：无 events 字段
    resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "session_id", "scene_id", "source_revision", "cursor_ms", "active",
    }

    # /events：字段不变
    body = client.post(f"/sessions/{sid}/events", json={"time_ms": 1000}).json()
    assert set(body.keys()) == {
        "session_id", "scene_id", "source_revision", "cursor_ms",
        "events", "active",
    }
    assert _triples([dict(e, seq=0) for e in body["events"]]) == [
        (0, 1000, "exit", "s1"),
        (0, 1000, "enter", "s3"),
    ]

    # /hidden_speakers：字段不变
    body = client.post(
        f"/sessions/{sid}/hidden_speakers", json={"hidden_speakers": ["Alice"]}
    ).json()
    assert set(body.keys()) == {
        "session_id", "scene_id", "source_revision", "cursor_ms",
        "hidden_speakers", "events", "active",
    }


def test_channel_serialized_with_session_operations():
    """并发推进与补取在会话锁内串行：补取返回的是某个一致前缀。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)

    results = []
    barrier = threading.Barrier(6)

    def advance_worker(time_ms):
        local = TestClient(app)
        barrier.wait()
        resp = local.post(f"/sessions/{sid}/events", json={"time_ms": time_ms})
        results.append(("advance", time_ms, resp.status_code))

    def ack_worker():
        local = TestClient(app)
        barrier.wait()
        resp = local.post(f"/sessions/{sid}/channel/ack", json={"ack_seq": 0})
        results.append(("ack", 0, resp.status_code))

    threads = [
        threading.Thread(target=advance_worker, args=(t,))
        for t in (800, 1000, 2500)
    ] + [threading.Thread(target=ack_worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 全部串行生效：推进成功或 422（倒退），补取全部成功
    assert all(status in (200, 422) for _, _, status in results)
    acks = [r for r in results if r[0] == "ack"]
    assert all(status == 200 for _, _, status in acks)

    # 最终补取：全部 4 个事件按 seq 连续排列，无丢失无重复
    # （s2 的退出在 3000，不在推进窗口内）
    final = ack(client, sid, 0).json()
    assert [e["seq"] for e in final["events"]] == [1, 2, 3, 4]
    assert _triples(final["events"]) == [
        (1, 800, "enter", "s2"),
        (2, 1000, "exit", "s1"),
        (3, 1000, "enter", "s3"),
        (4, 2500, "exit", "s3"),
    ]


def test_concurrent_opens_at_most_one_success():
    """并发开启通道：恰好一次成功，其余 409。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]

    results = []
    barrier = threading.Barrier(4)

    def worker():
        local = TestClient(app)
        barrier.wait()
        results.append(local.post(f"/sessions/{sid}/channel").status_code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 409, 409, 409]
