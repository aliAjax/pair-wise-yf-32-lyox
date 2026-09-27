import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, OrganAllocationService, cold_window, iso, utcnow


class OrganFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db"); self.now = utcnow()

    def tearDown(self): self.tmp.cleanup()

    def donor(self, expires_days=2):
        return self.svc.register_donor("coord", "coordinator", {"blood_type": "O", "organ": "kidney", "hospital": "H1", "region": "East", "available_at": iso(self.now - timedelta(days=3)), "expires_at": iso(self.now + timedelta(days=expires_days)), "clinical_match": 8})

    def candidate(self, name="患者甲", hospital="H2", urgency=5, wait=500):
        return self.svc.register_candidate("coord", "coordinator", {"patient_name": name, "blood_type": "B", "organ": "kidney", "hospital": hospital, "region": "East", "urgency": urgency, "wait_days": wait, "willing": True, "clinical_match": 9})

    def test_complete_allocation_and_cold_chain_flow(self):
        donor, candidate = self.donor(), self.candidate()
        rank = self.svc.ranking(donor["id"], "allocation_officer", "")
        self.assertEqual(rank["candidates"][0]["id"], candidate["id"])
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        accepted = self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(accepted["status"], "accepted")
        transit = self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.5})
        self.assertEqual(transit["status"], "in_transit")
        handoff = self.svc.initiate_handoff(allocation["id"], "hospital-h1", "hospital", "H1", {"expected_revision": transit["revision"], "to_hospital": "H2", "cold_chain_temp": 3.0})
        self.assertEqual(handoff["handoff"]["status"], "initiated")
        received = self.svc.accept_handoff(allocation["id"], "hospital-h2", "hospital", "H2", {})
        self.assertEqual(received["status"], "handed_off")
        implanted = self.svc.implant(allocation["id"], "allocator", "allocation_officer", {})
        self.assertEqual(implanted["status"], "implanted")
        audit = self.svc.audit(allocation["id"], "auditor")
        self.assertEqual([item["action"] for item in audit], ["allocation_proposed", "allocation_accepted", "transfer_started", "handoff_initiated", "handoff_accepted", "organ_implanted"])

    def test_expiry_privacy_and_single_allocation(self):
        expired = self.donor(expires_days=-1); candidate = self.candidate()
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": expired["id"], "candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "organ_expired")
        donor2 = self.donor(); allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor2["id"], "candidate_id": candidate["id"]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "wrong", "hospital", "H1", {"expected_revision": 1})
        self.assertEqual(ctx.exception.status, 403)
        masked = self.svc.get_allocation(allocation["id"], "hospital", "H1")
        self.assertEqual(masked["patient_name"], "***")
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": donor2["id"], "candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "donor_unavailable")
        other = self.candidate("患者乙", "H2", 4, 300)
        self.assertNotEqual(other["id"], candidate["id"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 12})
        self.assertEqual(ctx.exception.code, "cold_chain_violation")


class ColdIschemiaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db"); self.now = utcnow()

    def tearDown(self): self.tmp.cleanup()

    def donor(self, elapsed_min=None, max_min=None):
        body = {"blood_type": "O", "organ": "kidney", "hospital": "H1", "region": "East",
                "available_at": iso(self.now - timedelta(days=3)), "expires_at": iso(self.now + timedelta(days=2)), "clinical_match": 8}
        if max_min is not None:
            body["explant_at"] = iso(self.now - timedelta(minutes=elapsed_min)); body["max_cold_minutes"] = max_min
        return self.svc.register_donor("coord", "coordinator", body)

    def candidate(self):
        return self.svc.register_candidate("coord", "coordinator", {"patient_name": "患者甲", "blood_type": "B", "organ": "kidney", "hospital": "H2", "region": "East", "urgency": 5, "wait_days": 500, "willing": True, "clinical_match": 9})

    def force_elapsed(self, donor_id, elapsed_min):
        self.svc.repo.conn.execute("UPDATE donors SET explant_at=? WHERE id=?", (iso(self.now - timedelta(minutes=elapsed_min)), donor_id))

    def donor_row(self, donor_id):
        return self.svc.repo.conn.execute("SELECT * FROM donors WHERE id=?", (donor_id,)).fetchone()

    def test_cold_window_tiers_and_boundary(self):
        base = self.now.replace(microsecond=0)  # iso() 会截断微秒，边界断言用整秒基准
        donor = {"explant_at": iso(base), "max_cold_minutes": 600}
        self.assertEqual(cold_window(donor, base + timedelta(minutes=300))["tier"], "normal")
        self.assertEqual(cold_window(donor, base + timedelta(minutes=540))["tier"], "normal")   # 剩余正好一成不算预警
        self.assertEqual(cold_window(donor, base + timedelta(minutes=541))["tier"], "warning")  # 剩余不足一成
        self.assertEqual(cold_window(donor, base + timedelta(minutes=600))["tier"], "exceeded")
        exceeded = cold_window(donor, base + timedelta(minutes=650))
        self.assertEqual(exceeded["overtime_minutes"], 50)
        self.assertIsNone(cold_window({"explant_at": None, "max_cold_minutes": None}))  # 旧记录

    def test_registration_validation_and_state_view(self):
        base = {"blood_type": "O", "organ": "kidney", "hospital": "H1", "region": "East",
                "available_at": iso(self.now - timedelta(days=1)), "expires_at": iso(self.now + timedelta(days=1))}
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_donor("coord", "coordinator", {**base, "explant_at": iso(self.now)})
        self.assertEqual(ctx.exception.code, "cold_fields_pair")
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_donor("coord", "coordinator", {**base, "max_cold_minutes": 600})
        self.assertEqual(ctx.exception.code, "cold_fields_pair")
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_donor("coord", "coordinator", {**base, "explant_at": iso(self.now), "max_cold_minutes": 0})
        self.assertEqual(ctx.exception.code, "invalid_cold_minutes")
        donor = self.donor(100, 600)
        self.assertEqual(donor["cold_ischemia"]["tier"], "normal")
        self.assertAlmostEqual(donor["cold_ischemia"]["remaining_minutes"], 500, delta=1)
        state = self.svc.state("allocation_officer", "")
        cold = next(d["cold_ischemia"] for d in state["donors"] if d["id"] == donor["id"])
        self.assertEqual(cold["tier"], "normal")
        warning = self.donor(550, 600)
        cold = next(d["cold_ischemia"] for d in self.svc.state("coordinator", "")["donors"] if d["id"] == warning["id"])
        self.assertEqual(cold["tier"], "warning")
        legacy = self.donor()
        self.assertIsNone(legacy["cold_ischemia"])  # 旧记录继续按原有效期办理

    def test_warning_accept_requires_dispatcher(self):
        donor, candidate = self.donor(550, 600), self.candidate()
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "cold_confirm_required")
        accepted = self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1, "dispatcher_confirm": "调度员-07"})
        self.assertEqual(accepted["status"], "accepted")
        audit = self.svc.audit(allocation["id"], "auditor")
        record = next(a for a in audit if a["action"] == "allocation_accepted")
        self.assertIn("调度员-07", record["detail_json"])

    def test_warning_handoff_requires_dispatcher(self):
        donor, candidate = self.donor(550, 600), self.candidate()
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        accepted = self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1, "dispatcher_confirm": "调度员-07"})
        transit = self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.5})
        with self.assertRaises(ApiError) as ctx:
            self.svc.initiate_handoff(allocation["id"], "hospital-h1", "hospital", "H1", {"expected_revision": transit["revision"], "to_hospital": "H2", "cold_chain_temp": 3.0})
        self.assertEqual(ctx.exception.code, "cold_confirm_required")
        handoff = self.svc.initiate_handoff(allocation["id"], "hospital-h1", "hospital", "H1",
                                            {"expected_revision": transit["revision"], "to_hospital": "H2", "cold_chain_temp": 3.0, "dispatcher_confirm": "调度员-08"})
        self.assertEqual(handoff["handoff"]["status"], "initiated")
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept_handoff(allocation["id"], "hospital-h2", "hospital", "H2", {})
        self.assertEqual(ctx.exception.code, "cold_confirm_required")
        received = self.svc.accept_handoff(allocation["id"], "hospital-h2", "hospital", "H2", {"dispatcher_confirm": "调度员-08"})
        self.assertEqual(received["status"], "handed_off")

    def test_exceeded_accept_marks_organ_failed(self):
        donor, candidate = self.donor(100, 600), self.candidate()
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        self.force_elapsed(donor["id"], 650)  # 超时 50 分钟
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1, "dispatcher_confirm": "调度员-07"})
        self.assertEqual(ctx.exception.code, "cold_ischemia_exceeded")
        row = self.donor_row(donor["id"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["cold_failed_stage"], "accept")
        self.assertGreaterEqual(row["cold_overtime_minutes"], 49)
        alloc = self.svc.repo.conn.execute("SELECT status FROM allocations WHERE id=?", (allocation["id"],)).fetchone()
        self.assertEqual(alloc["status"], "expired")
        audit = self.svc.donor_audit(donor["id"], "auditor")
        failed = next(a for a in audit if a["action"] == "cold_ischemia_failed")
        self.assertIn('"accept"', failed["detail_json"])
        self.assertIn("overtime_minutes", failed["detail_json"])
        with self.assertRaises(ApiError) as ctx:  # 失效后任何继续流转都被拒
            self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "allocation_closed")

    def test_exceeded_implant_and_handoff_refused(self):
        donor, candidate = self.donor(100, 600), self.candidate()
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        accepted = self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        transit = self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.5})
        self.svc.initiate_handoff(allocation["id"], "hospital-h1", "hospital", "H1", {"expected_revision": transit["revision"], "to_hospital": "H2", "cold_chain_temp": 3.0})
        self.svc.accept_handoff(allocation["id"], "hospital-h2", "hospital", "H2", {})
        self.force_elapsed(donor["id"], 700)
        with self.assertRaises(ApiError) as ctx:
            self.svc.implant(allocation["id"], "allocator", "allocation_officer", {})
        self.assertEqual(ctx.exception.code, "cold_ischemia_exceeded")
        row = self.donor_row(donor["id"])
        self.assertEqual((row["status"], row["cold_failed_stage"]), ("failed", "implant"))
        self.assertGreaterEqual(row["cold_overtime_minutes"], 99)

    def test_exceeded_propose_refused_and_audited(self):
        donor, candidate = self.donor(650, 600), self.candidate()
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "cold_ischemia_exceeded")
        row = self.donor_row(donor["id"])
        self.assertEqual((row["status"], row["cold_failed_stage"]), ("failed", "propose"))
        audit = self.svc.donor_audit(donor["id"], "coordinator")
        self.assertIn("cold_ischemia_failed", [a["action"] for a in audit])
        with self.assertRaises(ApiError) as ctx:  # 失效器官不能再被提出
            self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "donor_unavailable")

    def test_legacy_donor_flow_unaffected(self):
        donor, candidate = self.donor(), self.candidate()  # 无冷缺血字段的旧记录
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        self.assertIsNone(allocation["cold_ischemia"])
        accepted = self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(accepted["status"], "accepted")  # 无需调度员确认


if __name__ == "__main__": unittest.main()
