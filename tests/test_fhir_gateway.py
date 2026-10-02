"""fhir-gateway 端到端测试。

覆盖：成功路径的字段顺序与不改写资源、三类校验异常、
AuditWriteError（参数缺失/目录/无权限）、退出码、stdout/stderr 形状、
审计行追加与已有行保留，以及真实子进程集成。
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from fhir_gateway.cli import main  # noqa: E402


def patient_request(**overrides):
    payload = {
        "resource": {"resourceType": "Patient", "id": "pat-1"},
        "term_maps": [],
        "audit_context": {
            "request_id": "req-1",
            "actor": "dr-who",
            "recorded_at": "2026-10-03T10:00:00Z",
        },
    }
    payload.update(overrides)
    return payload


def observation_request():
    return {
        "resource": {
            "resourceType": "Observation",
            "id": "obs-1",
            "status": "final",
            "code": {
                "coding": [
                    {"system": "http://loinc.org", "code": "29463-7"},
                    {"system": "http://local", "code": "weight-local"},
                ]
            },
        },
        "term_maps": [
            {
                "system": "http://loinc.org",
                "code": "29463-7",
                "target_system": "http://snomed.info/sct",
                "target_code": "27113001",
            }
        ],
        "audit_context": {
            "request_id": "req-2",
            "actor": "nurse",
            "recorded_at": "2026-10-03T10:01:00Z",
        },
    }


def condition_request():
    return {
        "resource": {
            "resourceType": "Condition",
            "id": "con-1",
            "clinicalStatus": {
                "coding": [
                    {"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}
                ]
            },
        },
        "term_maps": [
            {
                "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                "code": "active",
                "target_system": "http://example.org/state",
                "target_code": "A",
            }
        ],
        "audit_context": {
            "request_id": "req-3",
            "actor": "dr-who",
            "recorded_at": "2026-10-03T10:02:00Z",
        },
    }


class GatewayTestBase(unittest.TestCase):
    def run_cli(self, payload, audit_path, raw=None, args=None):
        stdin = io.StringIO(raw if raw is not None else json.dumps(payload))
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = args if args is not None else ["--audit-file", audit_path]
        code = main(argv=argv, stdin=stdin, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def assertErrorEnvelope(self, stderr_text, expected_type):
        self.assertTrue(stderr_text.endswith("\n"))
        envelope = json.loads(stderr_text)
        self.assertEqual(set(envelope.keys()), {"error"})
        error = envelope["error"]
        self.assertEqual(set(error.keys()), {"type", "message"})
        self.assertEqual(error["type"], expected_type)
        self.assertIsInstance(error["message"], str)
        self.assertTrue(error["message"])


class SuccessTests(GatewayTestBase):
    def test_patient_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = os.path.join(tmp, "audit.jsonl")
            code, out, err = self.run_cli(patient_request(), audit_path)

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertTrue(out.endswith("\n"))
        result = json.loads(out)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["resource_type"], "Patient")
        self.assertEqual(result["resource"], {"resourceType": "Patient", "id": "pat-1"})
        self.assertEqual(result["mappings"], [])

        audit = result["audit"]
        self.assertEqual(audit["request_id"], "req-1")
        self.assertEqual(audit["actor"], "dr-who")
        self.assertEqual(audit["recorded_at"], "2026-10-03T10:00:00Z")
        self.assertEqual(audit["decision"], "accepted")
        self.assertTrue(audit["audit_id"])
        self.assertIsInstance(audit["audit_id"], str)

    def test_output_field_order_is_fixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = os.path.join(tmp, "audit.jsonl")
            code, out, _ = self.run_cli(patient_request(), audit_path)

        self.assertEqual(code, 0)
        positions = [out.index('"%s"' % name) for name in
                     ("status", "resource_type", "resource", "mappings", "audit")]
        self.assertEqual(positions, sorted(positions))

    def test_mappings_hit_miss_and_resource_unchanged(self):
        payload = observation_request()
        resource_before = json.loads(json.dumps(payload["resource"]))
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = os.path.join(tmp, "audit.jsonl")
            code, out, err = self.run_cli(payload, audit_path)

        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["resource"], resource_before)
        mappings = result["mappings"]
        self.assertEqual(len(mappings), 2)

        self.assertEqual(mappings[0]["source"],
                         {"system": "http://loinc.org", "code": "29463-7"})
        self.assertEqual(mappings[0]["status"], "mapped")
        self.assertEqual(mappings[0]["target"],
                         {"system": "http://snomed.info/sct", "code": "27113001"})

        self.assertEqual(mappings[1]["source"],
                         {"system": "http://local", "code": "weight-local"})
        self.assertEqual(mappings[1]["status"], "unmapped")
        self.assertIsNone(mappings[1]["target"])

        # 映射结果内字段顺序固定。
        m0 = out.split('"mappings"', 1)[1]
        self.assertLess(m0.index('"source"'), m0.index('"status"'))
        self.assertLess(m0.index('"status"'), m0.index('"target"'))

    def test_condition_mapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = os.path.join(tmp, "audit.jsonl")
            code, out, err = self.run_cli(condition_request(), audit_path)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["resource_type"], "Condition")
        self.assertEqual(result["mappings"][0]["status"], "mapped")
        self.assertEqual(result["mappings"][0]["target"]["code"], "A")

    def test_equals_sign_argument_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = os.path.join(tmp, "audit.jsonl")
            code, _, err = self.run_cli(
                patient_request(), audit_path,
                args=["--audit-file=%s" % audit_path],
            )
        self.assertEqual(code, 0, err)


class ValidationErrorTests(GatewayTestBase):
    def expect_failure(self, payload, expected_type, tmp):
        audit_path = os.path.join(tmp, "audit.jsonl")
        code, out, err = self.run_cli(payload, audit_path)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertErrorEnvelope(err, expected_type)
        self.assertFalse(os.path.exists(audit_path))

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = os.path.join(tmp, "audit.jsonl")
            code, out, err = self.run_cli(
                None, audit_path, raw="{not json"
            )
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertErrorEnvelope(err, "InputError")
            self.assertFalse(os.path.exists(audit_path))

    def test_request_not_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = os.path.join(tmp, "audit.jsonl")
            code, out, err = self.run_cli(
                None, audit_path, raw="[1,2,3]"
            )
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertErrorEnvelope(err, "InputError")

    def test_missing_top_level_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            for key in ("resource", "term_maps", "audit_context"):
                payload = patient_request()
                del payload[key]
                self.expect_failure(payload, "InputError", tmp)

    def test_audit_context_bad(self):
        with tempfile.TemporaryDirectory() as tmp:
            for key in ("request_id", "actor", "recorded_at"):
                payload = patient_request()
                del payload["audit_context"][key]
                self.expect_failure(payload, "InputError", tmp)

                payload = patient_request()
                payload["audit_context"][key] = ""
                self.expect_failure(payload, "InputError", tmp)

    def test_resource_bad_type_and_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = patient_request()
            payload["resource"] = {"resourceType": "Medication", "id": "m1"}
            self.expect_failure(payload, "FhirValidationError", tmp)

            payload = patient_request()
            payload["resource"] = {"resourceType": "Patient", "id": ""}
            self.expect_failure(payload, "FhirValidationError", tmp)

            payload = patient_request()
            payload["resource"] = {"resourceType": "Patient"}
            self.expect_failure(payload, "FhirValidationError", tmp)

    def test_observation_bad_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = observation_request()
            payload["resource"]["status"] = "bogus"
            self.expect_failure(payload, "FhirValidationError", tmp)

    def test_observation_code_bad_coding(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = observation_request()
            payload["resource"]["code"]["coding"][0]["code"] = ""
            self.expect_failure(payload, "FhirValidationError", tmp)

            payload = observation_request()
            del payload["resource"]["code"]["coding"][0]["system"]
            self.expect_failure(payload, "FhirValidationError", tmp)

            payload = observation_request()
            payload["resource"]["code"]["coding"] = []
            self.expect_failure(payload, "FhirValidationError", tmp)

    def test_condition_clinical_status_bad(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = condition_request()
            payload["resource"]["clinicalStatus"]["coding"][0]["system"] = ""
            self.expect_failure(payload, "FhirValidationError", tmp)

            payload = condition_request()
            payload["resource"]["clinicalStatus"] = {}
            self.expect_failure(payload, "FhirValidationError", tmp)

    def test_term_map_invalid_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = observation_request()
            payload["term_maps"][0]["target_code"] = ""
            self.expect_failure(payload, "TermMappingError", tmp)

            payload = observation_request()
            del payload["term_maps"][0]["code"]
            self.expect_failure(payload, "TermMappingError", tmp)

            payload = observation_request()
            payload["term_maps"] = ["nope"]
            self.expect_failure(payload, "TermMappingError", tmp)

    def test_term_map_multiple_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = observation_request()
            payload["term_maps"].append({
                "system": "http://loinc.org",
                "code": "29463-7",
                "target_system": "http://other",
                "target_code": "999",
            })
            self.expect_failure(payload, "TermMappingError", tmp)

    def test_term_map_duplicate_same_target_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = observation_request()
            payload["term_maps"].append(dict(payload["term_maps"][0]))
            audit_path = os.path.join(tmp, "audit.jsonl")
            code, out, err = self.run_cli(payload, audit_path)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["mappings"][0]["status"], "mapped")


class AuditFileTests(GatewayTestBase):
    def test_audit_file_created_appended_and_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = os.path.join(tmp, "audit.jsonl")
            with open(audit_path, "w", encoding="utf-8") as handle:
                handle.write('{"preexisting": true}\n')

            code, out, err = self.run_cli(patient_request(), audit_path)
            self.assertEqual(code, 0, err)
            result = json.loads(out)

            code, _, err = self.run_cli(
                patient_request(audit_context={
                    "request_id": "req-1b", "actor": "actor-2",
                    "recorded_at": "2026-10-03T11:00:00Z"}),
                audit_path,
            )
            self.assertEqual(code, 0, err)

            with open(audit_path, encoding="utf-8") as handle:
                lines = [json.loads(line) for line in handle if line.strip()]

        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], {"preexisting": True})
        for line in lines[1:]:
            self.assertNotIn("resource", line)
            self.assertNotIn("mappings", line)
            self.assertEqual(line["decision"], "accepted")
            self.assertTrue(line["audit_id"])
        self.assertEqual(lines[1]["request_id"], "req-1")
        self.assertEqual(lines[2]["request_id"], "req-1b")
        # stdout 的 audit 对象与落盘行一致。
        self.assertEqual(lines[1], result["audit"])

    def test_missing_audit_file_parameter(self):
        stdin = io.StringIO(json.dumps(patient_request()))
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(argv=[], stdin=stdin, stdout=stdout, stderr=stderr)
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertErrorEnvelope(stderr.getvalue(), "AuditWriteError")

    def test_audit_path_is_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self.run_cli(patient_request(), tmp)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertErrorEnvelope(err, "AuditWriteError")

    @unittest.skipIf(
        not hasattr(os, "geteuid") or os.geteuid() == 0,
        "permission test is meaningless for root",
    )
    def test_audit_path_unwritable(self):
        with tempfile.TemporaryDirectory() as tmp:
            locked = os.path.join(tmp, "locked")
            os.mkdir(locked)
            os.chmod(locked, 0o500)
            audit_path = os.path.join(locked, "audit.jsonl")
            try:
                code, out, err = self.run_cli(patient_request(), audit_path)
            finally:
                os.chmod(locked, 0o700)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertErrorEnvelope(err, "AuditWriteError")

    def test_audit_write_failure_leaves_stdout_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            locked = os.path.join(tmp, "locked")
            os.mkdir(locked)
            os.chmod(locked, 0o500)
            target = os.path.join(locked, "audit.jsonl")
            try:
                code, out, err = self.run_cli(patient_request(), target)
            finally:
                os.chmod(locked, 0o700)
            if os.geteuid() == 0:
                self.skipTest("root bypasses permission bits")
            self.assertEqual(code, 2)
            self.assertEqual(out, "")


class SubprocessTests(unittest.TestCase):
    """真实进程集成：验证 shebang、退出码与管道 I/O。"""

    def test_subprocess_success_and_failure(self):
        script = os.path.join(REPO_ROOT, "bin", "fhir-gateway")
        env = dict(os.environ, PYTHONPATH=REPO_ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = os.path.join(tmp, "audit.jsonl")

            proc = subprocess.run(
                [sys.executable, script, "--audit-file", audit_path],
                input=json.dumps(patient_request()),
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["status"], "accepted")
            self.assertTrue(os.path.exists(audit_path))

            proc = subprocess.run(
                [sys.executable, script, "--audit-file", audit_path],
                input="not-json",
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            error = json.loads(proc.stderr)["error"]
            self.assertEqual(error["type"], "InputError")
            self.assertTrue(error["message"])

            # 失败请求不产生额外审计行。
            with open(audit_path, encoding="utf-8") as handle:
                self.assertEqual(len(handle.read().strip().splitlines()), 1)

    def test_subprocess_missing_parameter(self):
        script = os.path.join(REPO_ROOT, "bin", "fhir-gateway")
        env = dict(os.environ, PYTHONPATH=REPO_ROOT)
        proc = subprocess.run(
            [sys.executable, script],
            input=json.dumps(patient_request()),
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            json.loads(proc.stderr)["error"]["type"], "AuditWriteError"
        )


if __name__ == "__main__":
    unittest.main()
