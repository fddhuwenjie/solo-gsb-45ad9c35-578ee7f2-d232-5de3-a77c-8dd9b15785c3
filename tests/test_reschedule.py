# -*- coding: utf-8 -*-
"""已登记航次改期（POST /api/voyages/<id>/reschedule）回归测试。

覆盖：
- 改期成功：保留航次编号 / 登记时间 / 原发布号，只替换该航次航线快照中的
  起飞时刻，并用当前工作区已核准禁飞区重新审查；时刻与快照在同一条
  UPDATE 中原子更新；
- 即使期间已有新发布，改期仍沿用原航次自身快照的航线几何，不跟随新发布；
- 航次不存在 / 已结束 / 所见原时刻已被他页改动（stale）/ 新时刻或原时刻
  无效 / 审查报错 / 审查冲突 → 拒绝且原时刻与快照不变，附原因与明细；
- 改期后的航次仍是未结束航次：禁飞区核准须按最终生效时刻复核，不漏检；
- 改期、结束与禁飞区核准交错（BEGIN IMMEDIATE 写事务串行）不漏检。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

# 干净航线：4km 直线（y=100），200m 等高，08:00 起飞
ROUTE = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [25],
    "waypoints": [
        {"id": "w1", "name": "A", "x": 100, "y": 100, "alt": 200},
        {"id": "w2", "name": "B", "x": 500, "y": 100, "alt": 200},
    ],
}
# 同位置、时窗 20:00–21:00：08:00 起飞不冲突，20:30 起飞冲突
NIGHT_NFZ = {
    "id": "nzN", "name": "夜间管控区", "enabled": True,
    "vertices": [[250, 50], [350, 50], [350, 150], [250, 150]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "2026-09-24T20:00", "endTime": "2026-09-24T21:00",
}
# 远离航线的合法禁飞区（无时间窗）
FAR_NFZ = {
    "id": "nzFar", "name": "远侧限制区", "enabled": True,
    "vertices": [[100, 500], [200, 500], [200, 600], [100, 600]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "", "endTime": "",
}
# y=400 的另一条干净航线（用于再发布）
ROUTE_Y400 = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [25],
    "waypoints": [
        {"id": "w1", "name": "C", "x": 100, "y": 400, "alt": 200},
        {"id": "w2", "name": "D", "x": 500, "y": 400, "alt": 200},
    ],
}


def clone(obj):
    return json.loads(json.dumps(obj))


class RescheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_path = app.DB_PATH
        app.DB_PATH = os.path.join(self.tmp, "test.db")
        app.init_db()
        self.client = app.app.test_client()

    def tearDown(self):
        app.DB_PATH = self.old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    # —— 辅助 ——
    def put_state(self, state):
        r = self.client.put("/api/state", json=state)
        self.assertEqual(r.status_code, 200)
        return r.get_json()["rev"]

    def save_version(self, state, name="v1"):
        r = self.client.post("/api/versions", json={"name": name, "state": state})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["id"]

    def approve(self, nfzs, rev, seq):
        return self.client.post("/api/nfz-approval", json={
            "nfzs": nfzs, "workspaceRev": rev, "releaseSeq": seq})

    def register(self, seq, dep):
        return self.client.post("/api/voyages", json={
            "releaseSeq": seq, "departureTime": dep})

    def reschedule(self, vid, new_dep, seen_dep):
        return self.client.post(f"/api/voyages/{vid}/reschedule", json={
            "departureTime": new_dep, "expectedDepartureTime": seen_dep})

    def end_voyage(self, vid):
        return self.client.post(f"/api/voyages/{vid}/end")

    def get_voyage(self, vid):
        voyages = self.client.get("/api/voyages").get_json()["voyages"]
        return next(v for v in voyages if v["id"] == vid)

    def ws_state(self):
        r = self.client.get("/api/state")
        return r.get_json(), int(r.headers["X-Workspace-Rev"])

    def current_release(self):
        return self.client.get("/api/release").get_json()

    def publish_route(self, route=ROUTE, nfzs=None):
        """发布 route（工作区禁飞区为 nfzs，默认空），返回 (vid, rev, seq=1)。"""
        rev = self.put_state({"route": {"waypoints": []}, "nfzs": nfzs or []})
        vid = self.save_version({"route": route, "nfzs": []})
        r = self.client.post("/api/release", json={
            "versionId": vid, "workspaceRev": rev, "releaseSeq": 0})
        self.assertEqual(r.status_code, 200, r.get_json())
        return vid, rev, 1

    # —— 成功路径 ——
    def test_reschedule_success_keeps_id_seq_created_at_and_geometry(self):
        _, rev, seq = self.publish_route()
        r = self.register(seq, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        before = self.get_voyage(1)

        r = self.reschedule(1, "2026-09-24T09:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        v = r.get_json()["voyage"]
        # 航次编号、原发布号、登记时间保留
        self.assertEqual(v["id"], 1)
        self.assertEqual(v["releaseSeq"], seq)
        self.assertEqual(v["createdAt"], before["createdAt"])
        self.assertTrue(v["open"])
        self.assertIsNone(v["endedAt"])
        # 时刻与快照中的起飞时刻一起替换
        self.assertEqual(v["departureTime"], "2026-09-24T09:00")
        self.assertEqual(v["routeSnapshot"]["departureTime"], "2026-09-24T09:00")
        # 航线几何/速度/走廊等全部沿用原快照
        self.assertEqual(v["routeSnapshot"]["waypoints"], ROUTE["waypoints"])
        self.assertEqual(v["routeSnapshot"]["speeds"], ROUTE["speeds"])
        self.assertEqual(v["routeSnapshot"]["unitScale"], ROUTE["unitScale"])
        self.assertEqual(v["routeSnapshot"]["corridorHalfWidth"],
                         ROUTE["corridorHalfWidth"])

        # 改期不改工作区与发布：修订号不变、工作区与发布快照均未被改写
        _, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev)
        self.assertEqual(self.current_release()["release"]["state"]["nfzs"], [])

    def test_reschedule_reanalyzes_against_current_approved_nfzs(self):
        # 先无禁飞区登记 08:00 航次；随后核准夜间窗（对 08:00/09:00 安全）。
        _, rev, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        r = self.approve([clone(NIGHT_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev2 = r.get_json()["rev"]

        # 改到窗外 09:00：用当前已核准禁飞区重审 → 通过
        r = self.reschedule(1, "2026-09-24T09:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())

        # 改进夜间窗 20:30：与当前已核准禁飞区冲突 → 409 附明细，时刻不变
        rel_before = self.current_release()["release"]
        r = self.reschedule(1, "2026-09-24T20:30", "2026-09-24T09:00")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_conflicts")
        self.assertTrue(body["entries"])
        self.assertEqual(body["first"]["nfzId"], "nzN")
        v = self.get_voyage(1)
        self.assertEqual(v["departureTime"], "2026-09-24T09:00")
        self.assertEqual(v["routeSnapshot"]["departureTime"], "2026-09-24T09:00")
        self.assertTrue(v["open"])
        # 工作区、发布未被失败的改期改动
        _, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev2)
        self.assertEqual(self.current_release()["release"], rel_before)

        # 改回窗外 07:30 → 成功，列表时刻随之更新
        r = self.reschedule(1, "2026-09-24T07:30", "2026-09-24T09:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.get_voyage(1)["departureTime"], "2026-09-24T07:30")

    def test_reschedule_uses_original_voyage_geometry_after_new_release(self):
        # 发布 #1：y=100 航线，登记 08:00 航次；核准夜间窗（对 08:00 安全）
        vid1, rev, _ = self.publish_route(route=ROUTE)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        r = self.approve([clone(NIGHT_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        # 再发布 #2：y=400 航线（几何完全不同；同一已核准夜间窗对两条
        # 航线的 08:00 快照都安全，故发布可通过）
        vid2 = self.save_version({"route": ROUTE_Y400, "nfzs": []}, name="v2")
        self.assertEqual(self.client.post("/api/release", json={
            "versionId": vid2, "workspaceRev": r.get_json()["rev"],
            "releaseSeq": 1}).status_code, 200)
        # 改期仍按航次自身 y=100 快照几何：把时刻改到夜间窗内 20:30
        # 必须冲突；若误用了新发布 y=400 几何则会漏检
        r = self.reschedule(1, "2026-09-24T20:30", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_conflicts")
        self.assertEqual(body["first"]["nfzId"], "nzN")
        # 未改成，几何仍是 y=100
        v = self.get_voyage(1)
        self.assertEqual(v["releaseSeq"], 1)
        self.assertEqual(v["routeSnapshot"]["waypoints"], ROUTE["waypoints"])
        # 改到窗外时刻成功后，几何依旧是原航次快照
        r = self.reschedule(1, "2026-09-24T09:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        v = self.get_voyage(1)
        self.assertEqual(v["releaseSeq"], 1)
        self.assertEqual(v["routeSnapshot"]["waypoints"], ROUTE["waypoints"])
        self.assertEqual(v["routeSnapshot"]["speeds"], ROUTE["speeds"])

    def test_reschedule_same_time_is_noop(self):
        _, _, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        r = self.reschedule(1, "2026-09-24T08:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.get_voyage(1)["departureTime"], "2026-09-24T08:00")

    # —— 拒绝情形：原时刻与快照不变 ——
    def test_reschedule_unknown_voyage_404(self):
        self.publish_route()
        r = self.reschedule(999, "2026-09-24T09:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["reason"], "voyage_not_found")

    def test_reschedule_ended_voyage_rejected_and_unchanged(self):
        _, _, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        self.assertEqual(self.end_voyage(1).status_code, 200)
        before = self.get_voyage(1)
        r = self.reschedule(1, "2026-09-24T09:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "voyage_ended")
        after = self.get_voyage(1)
        self.assertEqual(after["departureTime"], before["departureTime"])
        self.assertEqual(after["routeSnapshot"], before["routeSnapshot"])
        self.assertFalse(after["open"])
        self.assertEqual(after["endedAt"], before["endedAt"])

    def test_reschedule_stale_when_original_time_changed_elsewhere(self):
        _, rev, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        # 他页（另一个客户端）先把航次改到 09:00
        other = app.app.test_client()
        r = other.post("/api/voyages/1/reschedule", json={
            "departureTime": "2026-09-24T09:00",
            "expectedDepartureTime": "2026-09-24T08:00"})
        self.assertEqual(r.status_code, 200, r.get_json())
        # 本页仍带所见原时刻 08:00 提交改期 → stale，明细给出期望/当前时刻，
        # 新时刻不生效、快照保持他页改期后的 09:00
        before = self.get_voyage(1)
        r = self.reschedule(1, "2026-09-24T10:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["departureTime"],
                         {"expected": "2026-09-24T08:00", "actual": "2026-09-24T09:00"})
        after = self.get_voyage(1)
        self.assertEqual(after["departureTime"], "2026-09-24T09:00")
        self.assertEqual(after["routeSnapshot"], before["routeSnapshot"])

    def test_reschedule_invalid_times(self):
        _, _, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        for bad_new in ("", None, "not-a-time", "2026-13-99T99:99"):
            r = self.client.post("/api/voyages/1/reschedule", json={
                "departureTime": bad_new,
                "expectedDepartureTime": "2026-09-24T08:00"})
            self.assertEqual(r.status_code, 400, bad_new)
            self.assertEqual(r.get_json()["reason"], "invalid_departure_time", bad_new)
        for bad_seen in ("", None, "not-a-time", "2026-13-99T99:99"):
            r = self.client.post("/api/voyages/1/reschedule", json={
                "departureTime": "2026-09-24T09:00",
                "expectedDepartureTime": bad_seen})
            self.assertEqual(r.status_code, 400, bad_seen)
            self.assertEqual(r.get_json()["reason"],
                             "invalid_expected_departure_time", bad_seen)
        v = self.get_voyage(1)
        self.assertEqual(v["departureTime"], "2026-09-24T08:00")
        self.assertEqual(v["routeSnapshot"]["departureTime"], "2026-09-24T08:00")

    def test_reschedule_analysis_error_leaves_voyage_untouched(self):
        _, _, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        before = self.get_voyage(1)
        # 直接把该航次快照破坏为“走廊半宽非正”：改期重审必报 route 错误
        conn = sqlite3.connect(app.DB_PATH)
        snap = json.loads(conn.execute(
            "SELECT route_snapshot FROM voyages WHERE id=1").fetchone()[0])
        snap["corridorHalfWidth"] = 0
        conn.execute("UPDATE voyages SET route_snapshot=? WHERE id=1",
                     (json.dumps(snap, ensure_ascii=False),))
        conn.commit()
        conn.close()

        r = self.reschedule(1, "2026-09-24T09:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_errors")
        self.assertTrue(body["errors"])
        # 时刻与快照均不变（坏快照也保持原样，不做半成功改写）
        conn = sqlite3.connect(app.DB_PATH)
        dep, raw = conn.execute(
            "SELECT departure_time, route_snapshot FROM voyages WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(dep, before["departureTime"])
        self.assertEqual(json.loads(raw)["corridorHalfWidth"], 0)

    # —— 改期后核准按最终时刻复核 ——
    def test_approval_rechecks_rescheduled_voyage_at_final_time(self):
        # 无禁飞区：08:00 航次登记成功；核准夜间窗（对 08:00 安全）通过
        _, rev, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        r = self.approve([clone(NIGHT_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 200, r.get_json())
        # 航次改期到 09:00（夜间窗外）：最终时刻 09:00 与发布快照 08:00
        # 对已核准夜间窗都安全
        r = self.reschedule(1, "2026-09-24T09:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        v = self.get_voyage(1)
        self.assertEqual(v["departureTime"], "2026-09-24T09:00")
        # 要把航次改到夜间窗内的 20:30，先清空工作区禁飞区（对任何时刻安全）
        r = self.approve([], self.current_release()["workspaceRev"], seq)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev_clear = r.get_json()["rev"]
        r = self.reschedule(1, "2026-09-24T20:30", "2026-09-24T09:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        # 此刻再提交夜间窗核准：发布快照航线 08:00 不冲突，但最终生效的
        # 未结束航次已改期到 20:30 → 必须 voyage_conflicts，不能按旧时刻漏检
        r = self.approve([clone(NIGHT_NFZ)], rev_clear, seq)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "voyage_conflicts")
        vcs = body["voyageConflicts"]
        self.assertEqual(len(vcs), 1)
        self.assertEqual(vcs[0]["voyageId"], 1)
        # 明细中的起飞时刻是改期后的最终时刻
        self.assertEqual(vcs[0]["departureTime"], "2026-09-24T20:30")
        self.assertEqual(vcs[0]["first"]["nfzId"], "nzN")
        # 航次结束后同一候选通过（改期后的航次结束后不再阻拦）
        self.assertEqual(self.end_voyage(1).status_code, 200)
        r = self.approve([clone(NIGHT_NFZ)], rev_clear, seq)
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_rescheduled_voyage_remains_open_and_counted_once(self):
        # 改期不新增航次、不结束航次；多次改期后核准仍只复核同一航次一次
        _, rev, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        self.assertEqual(
            self.reschedule(1, "2026-09-24T08:30", "2026-09-24T08:00").status_code,
            200)
        self.assertEqual(
            self.reschedule(1, "2026-09-24T09:00", "2026-09-24T08:30").status_code,
            200)
        voyages = self.client.get("/api/voyages").get_json()["voyages"]
        self.assertEqual([v["id"] for v in voyages], [1])
        self.assertTrue(voyages[0]["open"])
        r = self.approve([clone(FAR_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 200, r.get_json())

    # —— 交错：改期 / 核准 / 结束写事务串行不漏检 ——
    def test_interleaved_reschedule_and_approval_never_misses(self):
        # 场景：无禁飞区登记 08:00 航次；两条线程分别
        # A) 把航次改期到 20:30
        # B) 核准夜间窗（压 y=100、20–21 点；发布快照 08:00 安全）
        # 两事务经 BEGIN IMMEDIATE 串行：
        #  - 先 A 后 B：B 的航次复核看到最终时刻 20:30 → voyage_conflicts；
        #  - 先 B 后 A：B 时航次仍为 08:00 → 核准通过，随后 A 拿已核准夜间窗
        #    重审 → analysis_conflicts。
        # 两种顺序都不得出现“改期成功且核准也成功”的漏检结果。
        _, rev, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)

        outcomes = []

        def do_reschedule():
            c = app.app.test_client()
            r = c.post("/api/voyages/1/reschedule", json={
                "departureTime": "2026-09-24T20:30",
                "expectedDepartureTime": "2026-09-24T08:00"})
            outcomes.append(("reschedule", r.status_code,
                             r.get_json().get("reason")))

        def do_approval():
            c = app.app.test_client()
            r = c.post("/api/nfz-approval", json={
                "nfzs": [clone(NIGHT_NFZ)], "workspaceRev": rev, "releaseSeq": seq})
            outcomes.append(("approval", r.status_code,
                             r.get_json().get("reason")))

        t1 = threading.Thread(target=do_reschedule)
        t2 = threading.Thread(target=do_approval)
        t1.start(); t2.start(); t1.join(); t2.join()

        by = {name: (code, reason) for name, code, reason in outcomes}
        resched_ok = by["reschedule"][0] == 200
        approval_ok = by["approval"][0] == 200
        self.assertFalse(
            resched_ok and approval_ok,
            f"漏检：改期与核准同时成功 outcomes={outcomes}")
        # 至少一方拒绝，且拒绝原因属于预期的两类冲突
        if not resched_ok:
            self.assertEqual(by["reschedule"][1], "analysis_conflicts")
        if not approval_ok:
            self.assertEqual(by["approval"][0], 409)
            self.assertIn(by["approval"][1],
                          ("voyage_conflicts", "analysis_conflicts"))

    def test_interleaved_reschedule_and_end(self):
        # 结束与改期交错：同一时刻只有一个写事务成功——
        # 结束先到则改期收到 voyage_ended；改期先到则结束幂等落到改期后的航次。
        _, _, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)

        outcomes = []
        barrier = threading.Barrier(2)

        def do_reschedule():
            c = app.app.test_client()
            barrier.wait()
            r = c.post("/api/voyages/1/reschedule", json={
                "departureTime": "2026-09-24T09:00",
                "expectedDepartureTime": "2026-09-24T08:00"})
            outcomes.append(("reschedule", r.status_code,
                             r.get_json().get("reason")))

        def do_end():
            c = app.app.test_client()
            barrier.wait()
            r = c.post("/api/voyages/1/end")
            outcomes.append(("end", r.status_code, r.get_json().get("reason")))

        t1 = threading.Thread(target=do_reschedule)
        t2 = threading.Thread(target=do_end)
        t1.start(); t2.start(); t1.join(); t2.join()

        by = {name: (code, reason) for name, code, reason in outcomes}
        v = self.get_voyage(1)
        if by["reschedule"][0] == 200:
            # 改期先成功：结束随后幂等落到该航次，时刻为改期后的值
            self.assertEqual(by["end"][0], 200)
            self.assertEqual(v["departureTime"], "2026-09-24T09:00")
            self.assertFalse(v["open"])
        else:
            # 结束先成功：改期必须被拒且时刻保持 08:00
            self.assertEqual(by["reschedule"][0], 409)
            self.assertEqual(by["reschedule"][1], "voyage_ended")
            self.assertEqual(v["departureTime"], "2026-09-24T08:00")
            self.assertFalse(v["open"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
