#!/usr/bin/env python3
"""fhir-gateway 端到端测试（标准库 unittest，通过子进程调用）。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY = os.path.join(HERE, "fhir-gateway")


def run(payload, audit_file=None, args=None, raw=None):
    cmd = [sys.executable, GATEWAY]
    if audit_file is not None:
        cmd += ["--audit-file", audit_file]
    if args:
        cmd += args
    stdin = raw if raw is not None else json.dumps(payload)
    proc = subprocess.run(
        cmd, input=stdin, capture_output=True, text=True, timeout=30
    )
    return proc


def base_request(**overrides):
    req = {
        "resource": {
            "resourceType": "Observation",
            "id": "obs-1",
            "status": "final",
            "code": {
                "coding": [
                    {"system": "http://loinc.org", "code": "8867-4"}
                ]
            },
        },
        "term_maps": [
            {
                "system": "http://loinc.org",
                "code": "8867-4",
                "target_system": "http://snomed.info/sct",
                "target_code": "8499000",
            }
        ],
        "audit_context": {
            "request_id": "req-1",
            "actor": "dr-house",
            "recorded_at": "2026-10-03T10:00:00Z",
        },
    }
    req.update(overrides)
    return req


class SuccessTests(unittest.TestCase):
    def test_accepted_mapped_and_audit_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run(base_request(), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")

            out = json.loads(proc.stdout)
            self.assertEqual(
                list(out.keys()),
                ["status", "resource_type", "resource", "mappings", "audit"],
            )
            self.assertEqual(out["status"], "accepted")
            self.assertEqual(out["resource_type"], "Observation")
            self.assertEqual(out["resource"]["id"], "obs-1")
            self.assertEqual(len(out["mappings"]), 1)
            m = out["mappings"][0]
            self.assertEqual(
                m["source"], {"system": "http://loinc.org", "code": "8867-4"}
            )
            self.assertEqual(m["status"], "mapped")
            self.assertEqual(
                m["target"],
                {"system": "http://snomed.info/sct", "code": "8499000"},
            )

            audit = out["audit"]
            self.assertEqual(
                set(audit),
                {"request_id", "actor", "recorded_at", "decision", "audit_id"},
            )
            self.assertEqual(audit["request_id"], "req-1")
            self.assertEqual(audit["actor"], "dr-house")
            self.assertEqual(audit["recorded_at"], "2026-10-03T10:00:00Z")
            self.assertEqual(audit["decision"], "accepted")
            self.assertTrue(audit["audit_id"])

            with open(audit_file, encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0], audit)
            self.assertNotIn("resource", lines[0])
            self.assertNotIn("mappings", lines[0])

    def test_unmapped_target_is_null_resource_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request()
            req["term_maps"] = []
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["mappings"][0]["status"], "unmapped")
            self.assertIsNone(out["mappings"][0]["target"])
            self.assertEqual(out["resource"], req["resource"])

    def test_patient_and_condition(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(
                resource={
                    "resourceType": "Patient",
                    "id": "pat-9",
                    "name": [{"family": "Doe"}],
                }
            )
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["resource_type"], "Patient")
            self.assertEqual(out["mappings"], [])

            req2 = base_request(
                resource={
                    "resourceType": "Condition",
                    "id": "cond-1",
                    "clinicalStatus": {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                "code": "active",
                            }
                        ]
                    },
                }
            )
            proc = run(req2, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["mappings"][0]["status"], "unmapped")
            with open(audit_file, encoding="utf-8") as fh:
                lines = fh.readlines()
            self.assertEqual(len(lines), 2)

    def test_audit_file_created_and_existing_lines_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            with open(audit_file, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"prior": True}) + "\n")

            proc = run(base_request(), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(audit_file, encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(lines[0], {"prior": True})
            self.assertEqual(lines[1]["decision"], "accepted")

    def test_nested_codings_all_mapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(
                resource={
                    "resourceType": "Observation",
                    "id": "obs-2",
                    "status": "preliminary",
                    "code": {
                        "coding": [
                            {"system": "http://loinc.org", "code": "8867-4"},
                            {"system": "http://loinc.org", "code": "29463-7"},
                        ]
                    },
                    "component": [
                        {
                            "code": {
                                "coding": [
                                    {"system": "http://loinc.org", "code": "29463-7"}
                                ]
                            }
                        }
                    ],
                }
            )
            req["term_maps"].append(
                {
                    "system": "http://loinc.org",
                    "code": "29463-7",
                    "target_system": "http://snomed.info/sct",
                    "target_code": "27113001",
                }
            )
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            statuses = [m["status"] for m in json.loads(proc.stdout)["mappings"]]
            self.assertEqual(statuses, ["mapped", "mapped", "mapped"])

    def test_duplicate_same_target_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request()
            req["term_maps"].append(dict(req["term_maps"][0]))
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)


class ErrorTests(unittest.TestCase):
    def assert_error(self, proc, expected_type, audit_file=None, file_exists=None):
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        err = json.loads(proc.stderr)
        self.assertEqual(err["error"]["type"], expected_type)
        self.assertTrue(err["error"]["message"])
        if file_exists is not None:
            self.assertEqual(os.path.exists(audit_file), file_exists)

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run(None, audit_file, raw="{not json")
            self.assert_error(proc, "InputError", audit_file, False)

    def test_request_not_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run(None, audit_file, raw="[1,2,3]")
            self.assert_error(proc, "InputError", audit_file, False)

    def test_missing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run({"resource": {}}, audit_file)
            self.assert_error(proc, "InputError", audit_file, False)

    def test_audit_context_missing_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request()
            del req["audit_context"]["actor"]
            proc = run(req, audit_file)
            self.assert_error(proc, "InputError", audit_file, False)

    def test_audit_context_empty_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request()
            req["audit_context"]["request_id"] = ""
            proc = run(req, audit_file)
            self.assert_error(proc, "InputError", audit_file, False)

    def test_bad_resource_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(resource={"resourceType": "Medication", "id": "x"})
            proc = run(req, audit_file)
            self.assert_error(proc, "FhirValidationError", audit_file, False)

    def test_missing_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(resource={"resourceType": "Patient"})
            proc = run(req, audit_file)
            self.assert_error(proc, "FhirValidationError", audit_file, False)

    def test_bad_observation_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request()
            req["resource"]["status"] = "draft"
            proc = run(req, audit_file)
            self.assert_error(proc, "FhirValidationError", audit_file, False)

    def test_observation_code_missing_system(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request()
            req["resource"]["code"]["coding"][0] = {"code": "8867-4"}
            proc = run(req, audit_file)
            self.assert_error(proc, "FhirValidationError", audit_file, False)

    def test_condition_clinical_status_empty_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(
                resource={
                    "resourceType": "Condition",
                    "id": "c1",
                    "clinicalStatus": {
                        "coding": [{"system": "http://x", "code": ""}]
                    },
                }
            )
            proc = run(req, audit_file)
            self.assert_error(proc, "FhirValidationError", audit_file, False)

    def test_term_map_invalid_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request()
            req["term_maps"][0]["target_code"] = ""
            proc = run(req, audit_file)
            self.assert_error(proc, "TermMappingError", audit_file, False)

    def test_term_map_conflicting_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request()
            conflict = dict(req["term_maps"][0])
            conflict["target_code"] = "9999999"
            req["term_maps"].append(conflict)
            proc = run(req, audit_file)
            self.assert_error(proc, "TermMappingError", audit_file, False)

    def test_missing_audit_file_arg(self):
        proc = run(base_request())
        self.assert_error(proc, "AuditWriteError")

    def test_audit_file_is_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(base_request(), tmp)
            self.assert_error(proc, "AuditWriteError")

    def test_audit_file_unwritable_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "missing-dir", "audit.jsonl")
            proc = run(base_request(), nested)
            self.assert_error(proc, "AuditWriteError", nested, False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
