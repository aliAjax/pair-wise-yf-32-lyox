import json, sqlite3, sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, OrganAllocationService, iso, utcnow


class ColdIschemiaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db"); self.now = utcnow()

    def tearDown(self): self.tmp.cleanup()

    def donor(self, tolerance=None, explant_min_ago=0):
        body = {"blood_type": "O", "organ": "kidney", "hospital": "H1", "region": "East",
                "available_at": iso(self.now - timedelta(days=3)), "expires_at": iso(self.now + timedelta(days=2)), "clinical_match": 8}
        if tolerance is not None:
            body["explant_at"] = iso(self.now - timedelta(minutes=explant_min_ago))
            body["max_tolerance_minutes"] = tolerance
        return self.svc.register_donor("coord", "coordinator", body)

    def candidate(self):
        return self.svc.register_candidate("coord", "coordinator", {"patient_name": "患者甲", "blood_type": "B", "organ": "kidney", "hospital": "H2", "region": "East", "urgency": 5, "wait_days": 500, "willing": True, "clinical_match": 9})

    def allocation(self, donor):
        return self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": self.candidate()["id"]})

    def backdate(self, donor_id, minutes):
        self.svc.repo.conn.execute("UPDATE donors SET explant_at=? WHERE id=?", (iso(self.now - timedelta(minutes=minutes)), donor_id))

    def audit_detail(self, allocation_id, action):
        entries = [a for a in self.svc.audit(allocation_id, "auditor") if a["action"] == action]
        self.assertEqual(len(entries), 1, f"应只有一条 {action} 审计")
        return json.loads(entries[0]["detail_json"])

    def test_levels_and_remaining_minutes(self):
        normal = self.donor(tolerance=600, explant_min_ago=60)
        edge = self.donor(tolerance=100, explant_min_ago=89)
        warning = self.donor(tolerance=100, explant_min_ago=95)
        exceeded = self.donor(tolerance=100, explant_min_ago=200)
        legacy = self.donor()
        info = {d["id"]: d["cold_ischemia"] for d in self.svc.state("allocation_officer", "")["donors"]}
        self.assertEqual(info[normal["id"]]["level"], "normal")
        self.assertGreater(info[normal["id"]]["remaining_minutes"], 500)
        self.assertEqual(info[edge["id"]]["level"], "normal")  # 剩余 11 分钟，超过一成
        self.assertEqual(info[warning["id"]]["level"], "warning")  # 剩余约 5 分钟，不足一成
        self.assertLessEqual(info[warning["id"]]["remaining_minutes"], 5)
        self.assertEqual(info[exceeded["id"]]["level"], "exceeded")
        self.assertGreaterEqual(info[exceeded["id"]]["overtime_minutes"], 99)
        self.assertIsNone(info[legacy["id"]])  # 旧记录继续按原有效期办理

    def test_registration_validation(self):
        base = {"blood_type": "O", "organ": "kidney", "hospital": "H1", "region": "East",
                "available_at": iso(self.now - timedelta(days=1)), "expires_at": iso(self.now + timedelta(days=1))}
        for extra, code in [
            ({"explant_at": iso(self.now)}, "cold_ischemia_incomplete"),
            ({"max_tolerance_minutes": 60}, "cold_ischemia_incomplete"),
            ({"explant_at": iso(self.now), "max_tolerance_minutes": 0}, "invalid_tolerance"),
            ({"explant_at": iso(self.now), "max_tolerance_minutes": -5}, "invalid_tolerance"),
            ({"explant_at": iso(self.now), "max_tolerance_minutes": "60"}, "invalid_tolerance"),
            ({"explant_at": iso(self.now), "max_tolerance_minutes": True}, "invalid_tolerance"),
        ]:
            with self.assertRaises(ApiError) as ctx:
                self.svc.register_donor("coord", "coordinator", {**base, **extra})
            self.assertEqual(ctx.exception.code, code, extra)

    def test_warning_accept_requires_coordinator_confirmation(self):
        allocation = self.allocation(self.donor(tolerance=100, explant_min_ago=95))
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "coordinator_confirmation_required")
        accepted = self.svc.accept(allocation["id"], "h2", "hospital", "H2", {"expected_revision": 1, "coordinator_confirmed_by": "dispatcher-7"})
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["cold_ischemia"]["level"], "warning")
        detail = self.audit_detail(allocation["id"], "coordinator_cold_ischemia_confirmed")
        self.assertEqual(detail["stage"], "accept")
        self.assertEqual(detail["coordinator"], "dispatcher-7")

    def test_exceeded_accept_marks_organ_failed(self):
        donor = self.donor(tolerance=100, explant_min_ago=200)
        allocation = self.allocation(donor)
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "h2", "hospital", "H2", {"expected_revision": 1, "coordinator_confirmed_by": "dispatcher-7"})
        self.assertEqual(ctx.exception.code, "cold_ischemia_exceeded")
        donor_row = [d for d in self.svc.state("allocation_officer", "")["donors"] if d["id"] == donor["id"]][0]
        self.assertEqual(donor_row["status"], "failed")
        self.assertEqual(self.svc.get_allocation(allocation["id"], "allocation_officer", "")["status"], "expired")
        detail = self.audit_detail(allocation["id"], "organ_cold_ischemia_failed")
        self.assertEqual(detail["stage"], "accept")
        self.assertGreaterEqual(detail["overtime_minutes"], 99)
        with self.assertRaises(ApiError):  # 已结束的分配无法继续操作
            self.svc.accept(allocation["id"], "h2", "hospital", "H2", {"expected_revision": 2})

    def test_warning_handoff_requires_confirmation_but_implant_free(self):
        donor = self.donor(tolerance=100, explant_min_ago=95)
        allocation = self.allocation(donor)
        self.svc.accept(allocation["id"], "h2", "hospital", "H2", {"expected_revision": 1, "coordinator_confirmed_by": "dispatcher-7"})
        transit = self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.5})
        handoff_body = {"expected_revision": transit["revision"], "to_hospital": "H2", "cold_chain_temp": 3.0}
        with self.assertRaises(ApiError) as ctx:
            self.svc.initiate_handoff(allocation["id"], "h1", "hospital", "H1", handoff_body)
        self.assertEqual(ctx.exception.code, "coordinator_confirmation_required")
        handoff = self.svc.initiate_handoff(allocation["id"], "h1", "hospital", "H1", {**handoff_body, "coordinator_confirmed_by": "dispatcher-7"})
        self.assertEqual(handoff["handoff"]["status"], "initiated")
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept_handoff(allocation["id"], "h2", "hospital", "H2", {})
        self.assertEqual(ctx.exception.code, "coordinator_confirmation_required")
        received = self.svc.accept_handoff(allocation["id"], "h2", "hospital", "H2", {"coordinator_confirmed_by": "dispatcher-7"})
        self.assertEqual(received["status"], "handed_off")
        implanted = self.svc.implant(allocation["id"], "allocator", "allocation_officer", {})  # 预警不阻断植入
        self.assertEqual(implanted["status"], "implanted")

    def test_exceeded_blocks_handoff_initiate(self):
        donor = self.donor(tolerance=1000, explant_min_ago=0)
        allocation = self.allocation(donor)
        self.svc.accept(allocation["id"], "h2", "hospital", "H2", {"expected_revision": 1})
        transit = self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.5})
        self.backdate(donor["id"], 2000)
        with self.assertRaises(ApiError) as ctx:
            self.svc.initiate_handoff(allocation["id"], "h1", "hospital", "H1", {"expected_revision": transit["revision"], "to_hospital": "H2", "cold_chain_temp": 3.0})
        self.assertEqual(ctx.exception.code, "cold_ischemia_exceeded")
        detail = self.audit_detail(allocation["id"], "organ_cold_ischemia_failed")
        self.assertEqual(detail["stage"], "handoff_initiate")
        self.assertGreaterEqual(detail["overtime_minutes"], 999)

    def test_exceeded_blocks_handoff_accept(self):
        donor = self.donor(tolerance=1000, explant_min_ago=0)
        allocation = self.allocation(donor)
        self.svc.accept(allocation["id"], "h2", "hospital", "H2", {"expected_revision": 1})
        transit = self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.5})
        self.svc.initiate_handoff(allocation["id"], "h1", "hospital", "H1", {"expected_revision": transit["revision"], "to_hospital": "H2", "cold_chain_temp": 3.0})
        self.backdate(donor["id"], 2000)
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept_handoff(allocation["id"], "h2", "hospital", "H2", {})
        self.assertEqual(ctx.exception.code, "cold_ischemia_exceeded")
        self.assertEqual(self.audit_detail(allocation["id"], "organ_cold_ischemia_failed")["stage"], "handoff_accept")

    def test_exceeded_blocks_implant(self):
        donor = self.donor(tolerance=1000, explant_min_ago=0)
        allocation = self.allocation(donor)
        self.svc.accept(allocation["id"], "h2", "hospital", "H2", {"expected_revision": 1})
        transit = self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.5})
        self.svc.initiate_handoff(allocation["id"], "h1", "hospital", "H1", {"expected_revision": transit["revision"], "to_hospital": "H2", "cold_chain_temp": 3.0})
        self.svc.accept_handoff(allocation["id"], "h2", "hospital", "H2", {})
        self.backdate(donor["id"], 2000)
        with self.assertRaises(ApiError) as ctx:
            self.svc.implant(allocation["id"], "allocator", "allocation_officer", {})
        self.assertEqual(ctx.exception.code, "cold_ischemia_exceeded")
        self.assertEqual(self.audit_detail(allocation["id"], "organ_cold_ischemia_failed")["stage"], "implant")
        donor_row = [d for d in self.svc.state("allocation_officer", "")["donors"] if d["id"] == donor["id"]][0]
        self.assertEqual(donor_row["status"], "failed")

    def test_legacy_donor_flow_needs_no_confirmation(self):
        allocation = self.allocation(self.donor())  # 未登记离体信息
        accepted = self.svc.accept(allocation["id"], "h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(accepted["status"], "accepted")
        self.assertIsNone(accepted["cold_ischemia"])

    def test_legacy_database_migrates(self):
        db = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE donors(
            id INTEGER PRIMARY KEY AUTOINCREMENT, blood_type TEXT NOT NULL, organ TEXT NOT NULL, hospital TEXT NOT NULL,
            region TEXT NOT NULL, available_at TEXT NOT NULL, expires_at TEXT NOT NULL, clinical_match INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'available', revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO donors(blood_type,organ,hospital,region,available_at,expires_at,clinical_match,created_by,created_at) VALUES('O','kidney','H1','East',?,?,0,'coord',?)",
                     (iso(self.now - timedelta(days=1)), iso(self.now + timedelta(days=1)), iso(self.now)))
        conn.commit(); conn.close()
        svc = OrganAllocationService(db)
        donors = svc.state("allocation_officer", "")["donors"]
        self.assertIsNone(donors[0]["cold_ischemia"])  # 旧记录按原有效期办理
        fresh = svc.register_donor("coord", "coordinator", {"blood_type": "A", "organ": "liver", "hospital": "H3", "region": "West",
                                                            "available_at": iso(self.now), "expires_at": iso(self.now + timedelta(days=1)),
                                                            "explant_at": iso(self.now), "max_tolerance_minutes": 480})
        self.assertEqual(fresh["cold_ischemia"]["level"], "normal")


if __name__ == "__main__": unittest.main()
