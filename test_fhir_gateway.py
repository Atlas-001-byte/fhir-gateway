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


def bundle_request(entries=None, bundle_id="bundle-1", bundle_type="collection", **overrides):
    bundle = {"resourceType": "Bundle", "id": bundle_id, "type": bundle_type}
    if entries is not None:
        bundle["entry"] = entries
    req = base_request(resource=bundle, term_maps=[])
    req.update(overrides)
    return req


def patient_entry(pid="pat-1"):
    return {"resource": {"resourceType": "Patient", "id": pid}}


def observation_entry(oid="obs-1", status="final", system="http://loinc.org", code="8867-4"):
    return {
        "resource": {
            "resourceType": "Observation",
            "id": oid,
            "status": status,
            "code": {"coding": [{"system": system, "code": code}]},
        }
    }


def condition_entry(cid="cond-1", code="active"):
    return {
        "resource": {
            "resourceType": "Condition",
            "id": cid,
            "clinicalStatus": {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                        "code": code,
                    }
                ]
            },
        }
    }


class BundleSuccessTests(unittest.TestCase):
    def test_bundle_accepted_with_entries_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request(
                [patient_entry(), observation_entry(), condition_entry()]
            )
            req["term_maps"] = [
                {
                    "system": "http://loinc.org",
                    "code": "8867-4",
                    "target_system": "http://snomed.info/sct",
                    "target_code": "8499000",
                }
            ]
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")

            out = json.loads(proc.stdout)
            self.assertEqual(
                list(out.keys()),
                ["status", "resource_type", "resource", "mappings", "audit"],
            )
            self.assertEqual(out["status"], "accepted")
            self.assertEqual(out["resource_type"], "Bundle")
            self.assertEqual(out["resource"], req["resource"])
            self.assertEqual(len(out["mappings"]), 2)
            self.assertEqual(out["mappings"][0]["status"], "mapped")
            self.assertEqual(
                out["mappings"][0]["target"],
                {"system": "http://snomed.info/sct", "code": "8499000"},
            )
            self.assertEqual(out["mappings"][1]["status"], "unmapped")
            self.assertIsNone(out["mappings"][1]["target"])

            audit = out["audit"]
            self.assertEqual(audit["decision"], "accepted")
            self.assertTrue(audit["audit_id"])
            self.assertNotIn("resource", audit)
            self.assertNotIn("mappings", audit)
            with open(audit_file, encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0], audit)

    def test_bundle_without_or_empty_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            for bundle in (
                bundle_request(),
                bundle_request(entries=[]),
            ):
                proc = run(bundle, audit_file)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                out = json.loads(proc.stdout)
                self.assertEqual(out["resource_type"], "Bundle")
                self.assertEqual(out["mappings"], [])
            with open(audit_file, encoding="utf-8") as fh:
                lines = fh.readlines()
            self.assertEqual(len(lines), 2)

    def test_bundle_all_types_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            for btype in (
                "document",
                "message",
                "transaction",
                "transaction-response",
                "batch",
                "batch-response",
                "history",
                "searchset",
                "collection",
            ):
                proc = run(bundle_request(bundle_type=btype), audit_file)
                self.assertEqual(proc.returncode, 0, (btype, proc.stderr))

    def test_bundle_mapping_order_and_duplicates_not_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                observation_entry("obs-1", code="8867-4"),
                observation_entry("obs-2", code="29463-7"),
                observation_entry("obs-3", code="8867-4"),
            ]
            req = bundle_request(entries)
            req["term_maps"] = [
                {
                    "system": "http://loinc.org",
                    "code": "8867-4",
                    "target_system": "http://snomed.info/sct",
                    "target_code": "8499000",
                }
            ]
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            mappings = json.loads(proc.stdout)["mappings"]
            self.assertEqual(
                [(m["source"]["code"], m["status"]) for m in mappings],
                [
                    ("8867-4", "mapped"),
                    ("29463-7", "unmapped"),
                    ("8867-4", "mapped"),
                ],
            )

    def test_bundle_unknown_fields_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request(
                [
                    {
                        "fullUrl": "urn:uuid:abc",
                        "resource": {
                            "resourceType": "Patient",
                            "id": "pat-1",
                            "extra": {"nested": [1, 2]},
                        },
                    }
                ]
            )
            req["resource"]["link"] = [{"relation": "self"}]
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["resource"], req["resource"])


class BundleErrorTests(unittest.TestCase):
    def assert_error(self, proc, expected_type, audit_file=None, file_exists=None):
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        err = json.loads(proc.stderr)
        self.assertEqual(err["error"]["type"], expected_type)
        self.assertTrue(err["error"]["message"])
        if file_exists is not None:
            self.assertEqual(os.path.exists(audit_file), file_exists)

    def test_bundle_missing_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request([patient_entry()], bundle_id=None)
            del req["resource"]["id"]
            proc = run(req, audit_file)
            self.assert_error(proc, "FhirValidationError", audit_file, False)

    def test_bundle_empty_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run(bundle_request([patient_entry()], bundle_id=""), audit_file)
            self.assert_error(proc, "FhirValidationError", audit_file, False)

    def test_bundle_bad_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run(
                bundle_request([patient_entry()], bundle_type="bogus"),
                audit_file,
            )
            self.assert_error(proc, "FhirValidationError", audit_file, False)

    def test_bundle_entry_not_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run(bundle_request([1]), audit_file)
            self.assert_error(proc, "FhirValidationError", audit_file, False)

    def test_bundle_entry_missing_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run(bundle_request([{"fullUrl": "urn:uuid:x"}]), audit_file)
            self.assert_error(proc, "FhirValidationError", audit_file, False)

    def test_bundle_entry_bad_resource_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request(
                [patient_entry(), {"resource": {"resourceType": "Medication", "id": "m"}}]
            )
            proc = run(req, audit_file)
            self.assert_error(proc, "FhirValidationError", audit_file, False)

    def test_bundle_entry_resource_missing_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request(
                [patient_entry(), {"resource": {"resourceType": "Patient"}}]
            )
            proc = run(req, audit_file)
            self.assert_error(proc, "FhirValidationError", audit_file, False)

    def test_bundle_entry_observation_bad_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request(
                [patient_entry(), observation_entry(status="draft")]
            )
            proc = run(req, audit_file)
            self.assert_error(proc, "FhirValidationError", audit_file, False)

    def test_bundle_entry_not_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request()
            req["resource"]["entry"] = {"resource": {"resourceType": "Patient", "id": "p"}}
            proc = run(req, audit_file)
            self.assert_error(proc, "FhirValidationError", audit_file, False)

    def test_bundle_term_map_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request([observation_entry()])
            req["term_maps"] = [
                {"system": "http://loinc.org", "code": "8867-4", "target_system": ""}
            ]
            proc = run(req, audit_file)
            self.assert_error(proc, "TermMappingError", audit_file, False)

    def test_bundle_term_map_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request([observation_entry()])
            req["term_maps"] = [
                {
                    "system": "http://loinc.org",
                    "code": "8867-4",
                    "target_system": "http://snomed.info/sct",
                    "target_code": "8499000",
                },
                {
                    "system": "http://loinc.org",
                    "code": "8867-4",
                    "target_system": "http://snomed.info/sct",
                    "target_code": "9999999",
                },
            ]
            proc = run(req, audit_file)
            self.assert_error(proc, "TermMappingError", audit_file, False)

    def test_bundle_audit_unwritable(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "missing-dir", "audit.jsonl")
            proc = run(bundle_request([patient_entry()]), nested)
            self.assert_error(proc, "AuditWriteError", nested, False)


def exec_entry(resource, method="POST", url=None):
    """构造带 request 的 transaction/batch entry；url 缺省为首段匹配的形式。"""
    if url is None:
        url = "%s/%s" % (resource.get("resourceType"), resource.get("id", "x"))
    return {"request": {"method": method, "url": url}, "resource": resource}


def exec_request(entries, bundle_type="transaction", **overrides):
    return bundle_request(entries, bundle_type=bundle_type, **overrides)


def patient_resource(pid="pat-1", **extra):
    res = {"resourceType": "Patient", "id": pid}
    res.update(extra)
    return res


def observation_resource(oid="obs-1", status="final", code="8867-4"):
    return {
        "resourceType": "Observation",
        "id": oid,
        "status": status,
        "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
    }


class ExecutionBundleTests(unittest.TestCase):
    def run_ok(self, req, audit_file):
        proc = run(req, audit_file)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        return json.loads(proc.stdout)

    def test_transaction_all_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = exec_request(
                [
                    exec_entry(patient_resource()),
                    exec_entry(observation_resource(), method="PUT"),
                ]
            )
            req["term_maps"] = [
                {
                    "system": "http://loinc.org",
                    "code": "8867-4",
                    "target_system": "http://snomed.info/sct",
                    "target_code": "8499000",
                }
            ]
            out = self.run_ok(req, audit_file)

            self.assertEqual(
                list(out.keys()),
                ["status", "resource_type", "resource", "mappings", "audit"],
            )
            self.assertEqual(out["status"], 200)
            self.assertEqual(out["resource_type"], "Bundle")

            resp = out["resource"]
            self.assertEqual(resp["resourceType"], "Bundle")
            self.assertEqual(resp["type"], "transaction-response")
            self.assertEqual([e["index"] for e in resp["entry"]], [0, 1])
            self.assertEqual(
                [e["resourceType"] for e in resp["entry"]],
                ["Patient", "Observation"],
            )
            self.assertEqual([e["status"] for e in resp["entry"]], [200, 200])
            self.assertTrue(all("outcome" not in e for e in resp["entry"]))

            self.assertEqual(len(out["mappings"]), 1)
            self.assertEqual(out["mappings"][0]["status"], "mapped")

            audit = out["audit"]
            self.assertEqual(audit["bundle_type"], "transaction")
            self.assertEqual(audit["entry_total"], 2)
            self.assertEqual(audit["entry_succeeded"], 2)
            self.assertEqual(audit["entry_failed"], 0)
            self.assertEqual(audit["status"], 200)
            self.assertEqual(audit["decision"], "accepted")
            self.assertEqual(
                [e["stage"] for e in audit["entries"]],
                ["completed", "completed"],
            )
            self.assertTrue(all(e["issues"] == [] for e in audit["entries"]))

            with open(audit_file, encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0], audit)

    def test_transaction_missing_request_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = exec_request(
                [
                    exec_entry(patient_resource()),
                    {"resource": observation_resource("obs-2")},
                ]
            )
            out = self.run_ok(req, audit_file)
            self.assertEqual(out["status"], 400)
            self.assertEqual(out["resource"]["type"], "transaction-response")

            entries = out["resource"]["entry"]
            self.assertEqual([e["status"] for e in entries], [200, 400])
            issue = entries[1]["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "invalid")
            self.assertEqual(issue["entryIndex"], 1)
            self.assertEqual(issue["expression"], ["Bundle.entry[1].request"])
            self.assertEqual(issue["severity"], "error")
            self.assertEqual(
                entries[1]["outcome"]["resourceType"], "OperationOutcome"
            )

            audit = out["audit"]
            self.assertEqual(audit["status"], 400)
            self.assertEqual(audit["decision"], "rejected")
            self.assertEqual(audit["entry_succeeded"], 1)
            self.assertEqual(audit["entry_failed"], 1)
            self.assertEqual(audit["entries"][1]["stage"], "request")
            self.assertEqual(audit["entries"][1]["issues"], ["invalid"])
            with open(audit_file, encoding="utf-8") as fh:
                lines = [l for l in fh if l.strip()]
            self.assertEqual(len(lines), 1)

    def test_transaction_missing_resource_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = exec_request(
                [{"request": {"method": "POST", "url": "Patient/pat-1"}}]
            )
            out = self.run_ok(req, audit_file)
            self.assertEqual(out["status"], 400)
            entry = out["resource"]["entry"][0]
            self.assertIsNone(entry["resourceType"])
            issue = entry["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "invalid")
            self.assertEqual(issue["expression"], ["Bundle.entry[0].resource"])

    def test_transaction_url_type_mismatch_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = exec_request(
                [exec_entry(patient_resource(), url="Observation/pat-1")]
            )
            out = self.run_ok(req, audit_file)
            self.assertEqual(out["status"], 400)
            issue = out["resource"]["entry"][0]["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "invalid")
            self.assertEqual(
                issue["expression"], ["Bundle.entry[0].request.url"]
            )

    def test_transaction_method_not_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            for method in ("GET", "DELETE", "post"):
                req = exec_request(
                    [exec_entry(patient_resource(), method=method)]
                )
                out = self.run_ok(req, audit_file)
                self.assertEqual(out["status"], 400, method)
                issue = out["resource"]["entry"][0]["outcome"]["issue"][0]
                self.assertEqual(issue["code"], "not-supported")
                self.assertEqual(
                    issue["expression"], ["Bundle.entry[0].request.method"]
                )
                self.assertEqual(
                    out["audit"]["entries"][0]["issues"], ["not-supported"]
                )

    def test_transaction_validation_failure_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = exec_request(
                [exec_entry(observation_resource(status="draft"))]
            )
            out = self.run_ok(req, audit_file)
            self.assertEqual(out["status"], 400)
            issue = out["resource"]["entry"][0]["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "processing")
            self.assertEqual(issue["expression"], ["Bundle.entry[0].resource"])
            self.assertEqual(out["audit"]["entries"][0]["stage"], "validation")

    def test_transaction_all_or_nothing_no_partial_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = exec_request(
                [
                    exec_entry(patient_resource()),
                    exec_entry(observation_resource(status="draft")),
                    exec_entry(patient_resource("pat-2")),
                ]
            )
            out = self.run_ok(req, audit_file)
            # 任一失败整体 400；逐项结果仅作诊断，不构成部分成功。
            self.assertEqual(out["status"], 400)
            self.assertEqual(
                [e["status"] for e in out["resource"]["entry"]],
                [200, 400, 200],
            )
            audit = out["audit"]
            self.assertEqual(audit["decision"], "rejected")
            self.assertEqual(audit["entry_total"], 3)
            self.assertEqual(audit["entry_succeeded"], 2)
            self.assertEqual(audit["entry_failed"], 1)

    def test_batch_entries_independent_overall_200(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = exec_request(
                [
                    exec_entry(patient_resource()),
                    {"resource": observation_resource("obs-2")},
                    exec_entry(patient_resource("pat-2")),
                ],
                bundle_type="batch",
            )
            out = self.run_ok(req, audit_file)
            self.assertEqual(out["status"], 200)
            self.assertEqual(out["resource"]["type"], "batch-response")
            self.assertEqual(
                [e["status"] for e in out["resource"]["entry"]],
                [200, 400, 200],
            )
            self.assertEqual(
                out["resource"]["entry"][1]["outcome"]["issue"][0]["code"],
                "invalid",
            )
            audit = out["audit"]
            self.assertEqual(audit["decision"], "accepted")
            self.assertEqual(audit["bundle_type"], "batch")
            self.assertEqual(audit["entry_succeeded"], 2)
            self.assertEqual(audit["entry_failed"], 1)

    def test_batch_duplicate_entries_processed_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = exec_request(
                [
                    exec_entry(observation_resource("obs-1")),
                    exec_entry(observation_resource("obs-1")),
                ],
                bundle_type="batch",
            )
            out = self.run_ok(req, audit_file)
            self.assertEqual(out["status"], 200)
            self.assertEqual(
                [e["index"] for e in out["resource"]["entry"]], [0, 1]
            )
            self.assertEqual(
                [e["status"] for e in out["resource"]["entry"]], [200, 200]
            )
            # 重复编码逐项列出、不合并。
            self.assertEqual(len(out["mappings"]), 2)
            self.assertEqual(out["audit"]["entry_total"], 2)
            self.assertEqual(out["audit"]["entry_succeeded"], 2)

    def test_entry_not_object_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = exec_request([1])
            out = self.run_ok(req, audit_file)
            self.assertEqual(out["status"], 400)
            issue = out["resource"]["entry"][0]["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "invalid")
            self.assertEqual(issue["expression"], ["Bundle.entry[0]"])

    def test_empty_entries_transaction_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            out = self.run_ok(exec_request([]), audit_file)
            self.assertEqual(out["status"], 200)
            self.assertEqual(out["resource"]["entry"], [])
            self.assertEqual(out["mappings"], [])
            audit = out["audit"]
            self.assertEqual(audit["entry_total"], 0)
            self.assertEqual(audit["entry_succeeded"], 0)
            self.assertEqual(audit["entry_failed"], 0)

    def test_audit_has_no_resource_or_mapping_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = exec_request(
                [
                    exec_entry(patient_resource(name=[{"family": "Doe"}])),
                    exec_entry(observation_resource()),
                ]
            )
            req["term_maps"] = [
                {
                    "system": "http://loinc.org",
                    "code": "8867-4",
                    "target_system": "http://snomed.info/sct",
                    "target_code": "8499000",
                }
            ]
            out = self.run_ok(req, audit_file)
            self.assertEqual(out["status"], 200)
            with open(audit_file, encoding="utf-8") as fh:
                raw = fh.read()
            self.assertNotIn("Doe", raw)
            self.assertNotIn("8867-4", raw)
            self.assertNotIn("snomed", raw)
            self.assertNotIn("resource", json.loads(raw)["entries"][0])

    def test_repeat_submission_distinct_audit_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = exec_request([exec_entry(patient_resource())])
            self.run_ok(req, audit_file)
            self.run_ok(req, audit_file)
            with open(audit_file, encoding="utf-8") as fh:
                lines = [json.loads(l) for l in fh if l.strip()]
            self.assertEqual(len(lines), 2)
            self.assertNotEqual(lines[0]["audit_id"], lines[1]["audit_id"])
            self.assertTrue(all(l["audit_id"] for l in lines))

    def test_audit_write_failure_empty_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "missing-dir", "audit.jsonl")
            req = exec_request([exec_entry(patient_resource())])
            proc = run(req, nested)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            err = json.loads(proc.stderr)
            self.assertEqual(err["error"]["type"], "AuditWriteError")

    def test_term_map_conflict_still_top_level_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = exec_request([exec_entry(observation_resource())])
            req["term_maps"] = [
                {
                    "system": "http://loinc.org",
                    "code": "8867-4",
                    "target_system": "http://snomed.info/sct",
                    "target_code": "8499000",
                },
                {
                    "system": "http://loinc.org",
                    "code": "8867-4",
                    "target_system": "http://snomed.info/sct",
                    "target_code": "9999999",
                },
            ]
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "TermMappingError"
            )
            self.assertFalse(os.path.exists(audit_file))

    def test_bundle_level_errors_still_top_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = exec_request([exec_entry(patient_resource())])
            del req["resource"]["id"]
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "FhirValidationError"
            )

            req = exec_request([exec_entry(patient_resource())])
            req["resource"]["entry"] = {"resource": patient_resource()}
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "FhirValidationError"
            )
            self.assertFalse(os.path.exists(audit_file))


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
