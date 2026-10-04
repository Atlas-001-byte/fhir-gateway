#!/usr/bin/env python3
"""fhir-gateway 端到端测试（标准库 unittest，通过子进程调用）。"""

import hashlib
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


CHAIN_ARGS = ["--audit-chain"]
CHECK_ARGS = ["--check-references"]


def run_chain(payload, audit_file, raw=None):
    return run(payload, audit_file, args=CHAIN_ARGS, raw=raw)


def run_check(payload, audit_file, raw=None):
    return run(payload, audit_file, args=CHECK_ARGS, raw=raw)


def run_verify(audit_file, stdin=subprocess.DEVNULL):
    """校验模式：默认不接管道（DEVNULL），顺带验证其不读取标准输入。"""
    cmd = [sys.executable, GATEWAY, "--verify-audit", "--audit-file", audit_file]
    return subprocess.run(
        cmd, stdin=stdin, capture_output=True, text=True, timeout=30
    )


def run_find(audit_file, filters=None, args=None, stdin=subprocess.DEVNULL):
    """只读检索模式：默认不接管道（DEVNULL），顺带验证其不读取标准输入。"""
    cmd = [sys.executable, GATEWAY, "--audit-find", "--audit-file", audit_file]
    if filters:
        cmd += filters
    if args:
        cmd += args
    return subprocess.run(
        cmd, stdin=stdin, capture_output=True, text=True, timeout=30
    )


GENESIS_DIGEST = "0" * 64


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def chain_digest(prev_digest, audit):
    body = {k: v for k, v in audit.items() if k != "audit_digest"}
    return hashlib.sha256(
        (prev_digest + "\n" + canonical(body)).encode("utf-8")
    ).hexdigest()


def make_chain_line(prev_digest, audit):
    record = {k: v for k, v in audit.items() if k != "audit_digest"}
    record["audit_digest"] = chain_digest(prev_digest, record)
    return canonical(record), record["audit_digest"]


def write_lines(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line.rstrip("\n") + "\n")


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


def tx_entry(resource, method="POST", url=None):
    if url is None:
        url = "%s/%s" % (resource.get("resourceType"), resource.get("id", "x"))
    return {"request": {"method": method, "url": url}, "resource": resource}


def patient_resource(pid="pat-1"):
    return {"resourceType": "Patient", "id": pid}


def observation_resource(oid="obs-1", status="final"):
    return {
        "resourceType": "Observation",
        "id": oid,
        "status": status,
        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
    }


def executable_request(entries, bundle_type="transaction", **overrides):
    req = bundle_request(entries, bundle_type=bundle_type)
    req.update(overrides)
    return req


class ExecutableBundleTests(unittest.TestCase):
    def test_transaction_all_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(patient_resource("pat-1")),
                tx_entry(observation_resource("obs-1")),
            ]
            proc = run(executable_request(entries), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")

            out = json.loads(proc.stdout)
            self.assertEqual(list(out.keys()), ["status", "resource_type", "resource", "audit"])
            self.assertEqual(out["status"], 200)
            self.assertEqual(out["resource_type"], "Bundle")

            bundle = out["resource"]
            self.assertEqual(bundle["resourceType"], "Bundle")
            self.assertEqual(bundle["type"], "transaction-response")
            self.assertEqual(len(bundle["entry"]), 2)
            self.assertEqual(
                [(e["resourceType"], e["status"]) for e in bundle["entry"]],
                [("Patient", 200), ("Observation", 200)],
            )
            for entry in bundle["entry"]:
                self.assertNotIn("outcome", entry)

            audit = out["audit"]
            self.assertEqual(audit["request_id"], "req-1")
            self.assertEqual(audit["bundle_type"], "transaction")
            self.assertEqual(audit["entry_total"], 2)
            self.assertEqual(audit["entry_succeeded"], 2)
            self.assertEqual(audit["entry_failed"], 0)
            self.assertEqual(audit["status"], 200)
            self.assertTrue(audit["audit_id"])
            self.assertEqual(
                audit["entries"],
                [
                    {
                        "index": 0,
                        "resource_type": "Patient",
                        "phase": "completed",
                        "status": 200,
                        "issue_code": None,
                    },
                    {
                        "index": 1,
                        "resource_type": "Observation",
                        "phase": "completed",
                        "status": 200,
                        "issue_code": None,
                    },
                ],
            )

            with open(audit_file, encoding="utf-8") as fh:
                raw = fh.read()
            lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0], audit)
            # 审计不含资源正文、标识或术语映射原文。
            self.assertNotIn("pat-1", raw)
            self.assertNotIn("loinc", raw)

    def test_transaction_missing_request_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(patient_resource("pat-1")),
                {"resource": patient_resource("pat-2")},
            ]
            proc = run(executable_request(entries), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 400)
            entry = out["resource"]["entry"][1]
            self.assertEqual(entry["status"], 400)
            issue = entry["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "invalid")
            self.assertEqual(issue["entry"], 1)
            self.assertEqual(issue["expression"], "Bundle.entry[1].request")
            self.assertEqual(out["resource"]["entry"][0]["status"], 200)

            audit = out["audit"]
            self.assertEqual(audit["entry_failed"], 1)
            self.assertEqual(audit["entry_succeeded"], 1)
            self.assertEqual(audit["status"], 400)
            self.assertEqual(audit["entries"][1]["phase"], "request")
            self.assertEqual(audit["entries"][1]["issue_code"], "invalid")

    def test_transaction_missing_resource_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [{"request": {"method": "POST", "url": "Patient/pat-1"}}]
            proc = run(executable_request(entries), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 400)
            entry = out["resource"]["entry"][0]
            self.assertIsNone(entry["resourceType"])
            issue = entry["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "invalid")
            self.assertEqual(issue["expression"], "Bundle.entry[0].resource")

    def test_transaction_url_type_mismatch_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [tx_entry(patient_resource("pat-1"), url="Observation/pat-1")]
            proc = run(executable_request(entries), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 400)
            issue = out["resource"]["entry"][0]["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "invalid")
            self.assertEqual(issue["expression"], "Bundle.entry[0].request.url")

    def test_transaction_method_not_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [tx_entry(patient_resource(), method="DELETE")]
            proc = run(executable_request(entries), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 400)
            issue = out["resource"]["entry"][0]["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "not-supported")
            self.assertEqual(issue["expression"], "Bundle.entry[0].request.method")

    def test_transaction_validation_failure_is_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [tx_entry(observation_resource(status="draft"))]
            proc = run(executable_request(entries), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 400)
            issue = out["resource"]["entry"][0]["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "processing")
            self.assertEqual(out["audit"]["entries"][0]["phase"], "validation")

    def test_transaction_mapping_failure_is_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = executable_request([tx_entry(observation_resource())])
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
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 400)
            issue = out["resource"]["entry"][0]["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "processing")
            self.assertEqual(out["audit"]["entries"][0]["phase"], "mapping")

    def test_transaction_all_or_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(patient_resource("pat-1")),
                tx_entry(patient_resource("pat-2"), method="DELETE"),
                tx_entry(patient_resource("pat-3")),
            ]
            proc = run(executable_request(entries), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            # 任一失败整体 400，不返回部分成功的整体状态。
            self.assertEqual(out["status"], 400)
            self.assertEqual(
                [e["status"] for e in out["resource"]["entry"]], [200, 400, 200]
            )
            self.assertEqual(out["audit"]["status"], 400)
            self.assertEqual(out["audit"]["entry_failed"], 1)

    def test_batch_independent_and_duplicates_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(patient_resource("pat-1")),
                tx_entry(patient_resource("pat-2"), method="DELETE"),
                tx_entry(patient_resource("pat-1")),
            ]
            proc = run(executable_request(entries, bundle_type="batch"), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            # batch 各项独立：失败项 400，其余继续，整体 200。
            self.assertEqual(out["status"], 200)
            self.assertEqual(out["resource"]["type"], "batch-response")
            self.assertEqual(
                [e["status"] for e in out["resource"]["entry"]], [200, 400, 200]
            )
            issue = out["resource"]["entry"][1]["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "not-supported")
            self.assertEqual(issue["entry"], 1)

            audit = out["audit"]
            self.assertEqual(audit["bundle_type"], "batch")
            self.assertEqual(audit["status"], 200)
            self.assertEqual(audit["entry_total"], 3)
            self.assertEqual(audit["entry_succeeded"], 2)
            self.assertEqual(audit["entry_failed"], 1)
            self.assertEqual(
                [e["status"] for e in audit["entries"]], [200, 400, 200]
            )

    def test_transaction_empty_entries_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run(executable_request([]), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 200)
            self.assertEqual(out["resource"]["entry"], [])
            self.assertEqual(out["audit"]["entry_total"], 0)

    def test_repeat_submission_independent_audit_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = executable_request([tx_entry(patient_resource())])
            for _ in range(2):
                proc = run(req, audit_file)
                self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(audit_file, encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(len(lines), 2)
            self.assertNotEqual(lines[0]["audit_id"], lines[1]["audit_id"])

    def test_executable_audit_write_failure_empty_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "missing-dir", "audit.jsonl")
            req = executable_request([tx_entry(patient_resource())])
            proc = run(req, nested)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            err = json.loads(proc.stderr)
            self.assertEqual(err["error"]["type"], "AuditWriteError")

    def test_executable_bundle_missing_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = executable_request([tx_entry(patient_resource())])
            del req["resource"]["id"]
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            err = json.loads(proc.stderr)
            self.assertEqual(err["error"]["type"], "FhirValidationError")
            self.assertFalse(os.path.exists(audit_file))

    def test_executable_term_maps_not_array_is_input_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = executable_request([tx_entry(patient_resource())], term_maps={})
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            err = json.loads(proc.stderr)
            self.assertEqual(err["error"]["type"], "InputError")
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


class AuditChainWriteTests(unittest.TestCase):
    def assert_error(self, proc, expected_type):
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        err = json.loads(proc.stderr)
        self.assertEqual(err["error"]["type"], expected_type)
        self.assertTrue(err["error"]["message"])

    def _read_lines(self, path):
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_single_resource_digest_consistent_with_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run_chain(base_request(), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            audit = out["audit"]
            self.assertRegex(audit["audit_digest"], r"^[0-9a-f]{64}$")
            # 既有字段保持不变，仅新增 audit_digest。
            self.assertEqual(
                set(audit),
                {
                    "request_id",
                    "actor",
                    "recorded_at",
                    "decision",
                    "audit_id",
                    "audit_digest",
                },
            )

            lines = self._read_lines(audit_file)
            self.assertEqual(len(lines), 1)
            # stdout 与落盘一致。
            self.assertEqual(lines[0], audit)
            # 独立复算：首条前序摘要为 64 个 0。
            self.assertEqual(
                chain_digest(GENESIS_DIGEST, audit), audit["audit_digest"]
            )

    def test_multiple_submissions_form_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            audits = []
            for i in range(3):
                req = base_request()
                req["audit_context"]["request_id"] = "req-%d" % i
                proc = run_chain(req, audit_file)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                audits.append(json.loads(proc.stdout)["audit"])

            with open(audit_file, encoding="utf-8") as fh:
                raw_lines = [line for line in fh.read().splitlines() if line]
            self.assertEqual(len(raw_lines), 3)

            prev = GENESIS_DIGEST
            for raw, audit in zip(raw_lines, audits):
                obj = json.loads(raw)
                self.assertEqual(obj, audit)
                self.assertEqual(chain_digest(prev, obj), obj["audit_digest"])
                prev = obj["audit_digest"]

            result = run_verify(audit_file)
            self.assertEqual(result.returncode, 0, result.stderr)
            body = json.loads(result.stdout)
            self.assertEqual(body["status"], "valid")
            self.assertEqual(body["line_count"], 3)
            self.assertEqual(body["last_audit_id"], audits[-1]["audit_id"])
            self.assertEqual(body["last_audit_digest"], audits[-1]["audit_digest"])

    def test_non_ascii_preserved_in_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request()
            req["audit_context"]["actor"] = "医生甲"
            proc = run_chain(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(audit_file, "rb") as fh:
                raw = fh.read()
            # 非 ASCII 原样落盘（UTF-8），不转义。
            self.assertIn("医生甲".encode("utf-8"), raw)
            obj = json.loads(raw.decode("utf-8"))
            self.assertEqual(chain_digest(GENESIS_DIGEST, obj), obj["audit_digest"])

    def test_transaction_bundle_chain_fields_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(patient_resource("pat-1")),
                tx_entry(observation_resource("obs-1")),
            ]
            proc = run_chain(executable_request(entries, "batch"), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            audit = out["audit"]
            self.assertRegex(audit["audit_digest"], r"^[0-9a-f]{64}$")
            # 批量摘要字段不变。
            self.assertEqual(audit["bundle_type"], "batch")
            self.assertEqual(audit["entry_total"], 2)
            self.assertEqual(audit["entry_succeeded"], 2)
            self.assertEqual(audit["entry_failed"], 0)
            self.assertEqual(len(audit["entries"]), 2)
            lines = self._read_lines(audit_file)
            self.assertEqual(lines[0], audit)
            self.assertEqual(
                chain_digest(GENESIS_DIGEST, audit), audit["audit_digest"]
            )

    def test_empty_and_missing_file_seed_genesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 不存在的文件直接创建。
            missing = os.path.join(tmp, "new.jsonl")
            proc = run_chain(base_request(), missing)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(os.path.exists(missing))
            # 空文件视为合法空链。
            empty = os.path.join(tmp, "empty.jsonl")
            open(empty, "w").close()
            proc = run_chain(base_request(), empty)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_rejects_plain_line_and_does_not_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            write_lines(audit_file, [json.dumps({"audit_id": "old", "decision": "x"})])
            proc = run_chain(base_request(), audit_file)
            self.assert_error(proc, "AuditWriteError")
            self.assertEqual(len(self._read_lines(audit_file)), 1)

    def test_rejects_tampered_digest_and_does_not_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            line, _ = make_chain_line(GENESIS_DIGEST, {"audit_id": "a1"})
            tampered = line.replace('"audit_digest":"', '"audit_digest":"0', 1)
            write_lines(audit_file, [tampered])
            proc = run_chain(base_request(), audit_file)
            self.assert_error(proc, "AuditWriteError")
            self.assertEqual(len(self._read_lines(audit_file)), 1)

    def test_rejects_non_object_and_broken_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "bad.jsonl")
            line, digest = make_chain_line(GENESIS_DIGEST, {"audit_id": "a1"})
            # 第二行摘要不接续第一行。
            bad2, _ = make_chain_line(GENESIS_DIGEST, {"audit_id": "a2"})
            write_lines(audit_file, [line, bad2])
            proc = run_chain(base_request(), audit_file)
            self.assert_error(proc, "AuditWriteError")

            nonobj = os.path.join(tmp, "nonobj.jsonl")
            write_lines(nonobj, ["[1,2]"])
            proc = run_chain(base_request(), nonobj)
            self.assert_error(proc, "AuditWriteError")

    def test_chain_path_errors_are_write_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_chain(base_request(), tmp)
            self.assert_error(proc, "AuditWriteError")
            nested = os.path.join(tmp, "missing-dir", "audit.jsonl")
            proc = run_chain(base_request(), nested)
            self.assert_error(proc, "AuditWriteError")

    def test_normal_mode_has_no_digest_and_keeps_arbitrary_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            write_lines(audit_file, [json.dumps({"prior": True})])
            proc = run(base_request(), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            audit = json.loads(proc.stdout)["audit"]
            self.assertNotIn("audit_digest", audit)
            lines = self._read_lines(audit_file)
            self.assertEqual(lines[0], {"prior": True})
            self.assertNotIn("audit_digest", lines[1])


class VerifyAuditTests(unittest.TestCase):
    def assert_read_error(self, proc, expected="AuditReadError"):
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        err = json.loads(proc.stderr)
        self.assertEqual(err["error"]["type"], expected)
        self.assertTrue(err["error"]["message"])

    def assert_verification_error(self, proc):
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        err = json.loads(proc.stderr)
        self.assertEqual(err["error"]["type"], "AuditVerificationError")
        self.assertTrue(err["error"]["message"])
        # stderr 恰好一行 JSON。
        self.assertEqual(proc.stderr.count("\n"), 1)

    def _seed_chain(self, path, records):
        prev = GENESIS_DIGEST
        lines = []
        for record in records:
            line, prev = make_chain_line(prev, record)
            lines.append(line)
        write_lines(path, lines)

    def test_valid_chain_output_and_key_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self._seed_chain(
                audit_file,
                [{"audit_id": "a1", "request_id": "r1"},
                 {"audit_id": "a2", "request_id": "r2"}],
            )
            result = run_verify(audit_file)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            body = json.loads(result.stdout)
            self.assertEqual(
                list(body.keys()),
                ["status", "line_count", "last_audit_id", "last_audit_digest"],
            )
            self.assertEqual(body["status"], "valid")
            self.assertEqual(body["line_count"], 2)
            self.assertEqual(body["last_audit_id"], "a2")
            self.assertRegex(body["last_audit_digest"], r"^[0-9a-f]{64}$")

    def test_empty_file_zero_count_null_tails(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "empty.jsonl")
            open(audit_file, "w").close()
            result = run_verify(audit_file)
            self.assertEqual(result.returncode, 0, result.stderr)
            body = json.loads(result.stdout)
            self.assertEqual(
                body,
                {
                    "status": "valid",
                    "line_count": 0,
                    "last_audit_id": None,
                    "last_audit_digest": None,
                },
            )

    def test_missing_file_is_read_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_verify(os.path.join(tmp, "nope.jsonl"))
            self.assert_read_error(result)

    def test_directory_is_read_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_verify(tmp)
            self.assert_read_error(result)

    def test_missing_parent_is_read_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_verify(os.path.join(tmp, "nodir", "x.jsonl"))
            self.assert_read_error(result)

    def _one_bad_line(self, content):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "bad.jsonl")
            with open(audit_file, "w", encoding="utf-8") as fh:
                fh.write(content)
            return run_verify(audit_file)

    def test_bad_json_is_verification_error(self):
        self.assert_verification_error(self._one_bad_line("{not json\n"))

    def test_non_object_line_is_verification_error(self):
        self.assert_verification_error(self._one_bad_line('["x"]\n'))

    def test_missing_digest_is_verification_error(self):
        self.assert_verification_error(
            self._one_bad_line(json.dumps({"audit_id": "a1"}) + "\n")
        )

    def test_malformed_digest_is_verification_error(self):
        self.assert_verification_error(
            self._one_bad_line(
                json.dumps({"audit_id": "a1", "audit_digest": "XYZ"}) + "\n"
            )
        )

    def test_empty_and_missing_audit_id_are_verification_error(self):
        self.assert_verification_error(
            self._one_bad_line(
                json.dumps({"audit_id": "", "audit_digest": "0" * 64}) + "\n"
            )
        )
        self.assert_verification_error(
            self._one_bad_line(
                json.dumps({"audit_digest": "0" * 64}) + "\n"
            )
        )

    def test_duplicate_audit_id_is_verification_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "dup.jsonl")
            l1, d1 = make_chain_line(GENESIS_DIGEST, {"audit_id": "same"})
            l2, _ = make_chain_line(d1, {"audit_id": "same", "request_id": "r2"})
            write_lines(audit_file, [l1, l2])
            self.assert_verification_error(run_verify(audit_file))

    def test_broken_chain_is_verification_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "broken.jsonl")
            line, d1 = make_chain_line(GENESIS_DIGEST, {"audit_id": "a1"})
            # 第二行格式合法，但前序摘要错用 genesis，链在第 2 行断裂。
            bad2, _ = make_chain_line(GENESIS_DIGEST, {"audit_id": "a2"})
            write_lines(audit_file, [line, bad2])
            self.assert_verification_error(run_verify(audit_file))

    def test_tampered_content_is_verification_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "tampered.jsonl")
            line, _ = make_chain_line(GENESIS_DIGEST, {"audit_id": "a1"})
            obj = json.loads(line)
            obj["request_id"] = "tampered"  # 改动正文但未重算摘要
            write_lines(audit_file, [canonical(obj)])
            self.assert_verification_error(run_verify(audit_file))

    def test_does_not_read_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self._seed_chain(audit_file, [{"audit_id": "a1"}])
            # 即使标准输入喂入数据，校验模式也忽略它。
            result = run_verify(audit_file, stdin=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["line_count"], 1)


class AuditFindTests(unittest.TestCase):
    def accepted(self, audit_id, request_id="r", actor="alice",
                 recorded_at="2026-10-01T10:00:00Z", **extra):
        rec = {
            "request_id": request_id,
            "actor": actor,
            "recorded_at": recorded_at,
            "decision": "accepted",
            "audit_id": audit_id,
        }
        rec.update(extra)
        return rec

    def rejected(self, audit_id, request_id=None, actor=None, recorded_at=None):
        return {
            "request_id": request_id,
            "actor": actor,
            "recorded_at": recorded_at,
            "decision": "rejected",
            "audit_id": audit_id,
            "error_type": "InputError",
            "phase": "request",
            "status": 400,
        }

    def seed(self, path, records):
        write_lines(path, [json.dumps(r, ensure_ascii=False) for r in records])

    def assert_ok(self, proc, expected_ids):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        body = json.loads(proc.stdout)
        self.assertEqual(list(body.keys()), ["count", "records"])
        self.assertEqual(body["count"], len(expected_ids))
        self.assertEqual(
            [r["audit_id"] for r in body["records"]], expected_ids
        )
        return body

    # ---- 基本检索：空条件、行序、不去重 ------------------------------------

    def test_no_filters_returns_all_valid_in_line_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(
                audit_file,
                [self.accepted("a1"), self.accepted("a2"), self.accepted("a3")],
            )
            self.assert_ok(run_find(audit_file), ["a1", "a2", "a3"])

    def test_duplicate_audit_id_not_deduped(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(audit_file, [self.accepted("dup"), self.accepted("dup")])
            self.assert_ok(run_find(audit_file), ["dup", "dup"])

    def test_empty_result_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "empty.jsonl")
            open(audit_file, "w").close()
            proc = run_find(audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                json.loads(proc.stdout), {"count": 0, "records": []}
            )

    def test_no_match_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(audit_file, [self.accepted("a1")])
            proc = run_find(audit_file, ["--actor", "nobody"])
            self.assertEqual(
                json.loads(proc.stdout), {"count": 0, "records": []}
            )

    # ---- 精确匹配 -----------------------------------------------------------

    def test_filter_request_id_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(
                audit_file,
                [
                    self.accepted("a1", request_id="req-1"),
                    self.accepted("a2", request_id="req-2"),
                    self.accepted("a3", request_id="req-1"),
                ],
            )
            self.assert_ok(
                run_find(audit_file, ["--request-id", "req-1"]), ["a1", "a3"]
            )

    def test_filter_actor_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(
                audit_file,
                [
                    self.accepted("a1", actor="alice"),
                    self.accepted("a2", actor="bob"),
                    self.accepted("a3", actor="alice"),
                ],
            )
            self.assert_ok(run_find(audit_file, ["--actor", "bob"]), ["a2"])

    def test_exact_match_is_not_substring(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(audit_file, [self.accepted("a1", actor="alice")])
            self.assert_ok(run_find(audit_file, ["--actor", "alic"]), [])

    def test_filter_decision_accepted_and_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(
                audit_file,
                [
                    self.accepted("a1"),
                    self.rejected("r1", request_id="r", actor="alice",
                                  recorded_at="2026-10-01T10:00:00Z"),
                    self.accepted("a2"),
                ],
            )
            self.assert_ok(
                run_find(audit_file, ["--decision", "accepted"]),
                ["a1", "a2"],
            )
            self.assert_ok(
                run_find(audit_file, ["--decision", "rejected"]), ["r1"]
            )

    def test_conditions_combined_with_and(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(
                audit_file,
                [
                    self.accepted("a1", request_id="r1", actor="alice"),
                    self.accepted("a2", request_id="r1", actor="bob"),
                    self.accepted("a3", request_id="r2", actor="alice"),
                ],
            )
            # request_id 与 actor 同时满足才命中。
            self.assert_ok(
                run_find(
                    audit_file,
                    ["--request-id", "r1", "--actor", "alice"],
                ),
                ["a1"],
            )

    # ---- 时间窗 -------------------------------------------------------------

    def test_from_to_inclusive_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(
                audit_file,
                [
                    self.accepted("lo", recorded_at="2026-10-01T09:59:59Z"),
                    self.accepted("from", recorded_at="2026-10-01T10:00:00Z"),
                    self.accepted("mid", recorded_at="2026-10-01T11:00:00Z"),
                    self.accepted("to", recorded_at="2026-10-01T12:00:00Z"),
                    self.accepted("hi", recorded_at="2026-10-01T12:00:01Z"),
                ],
            )
            args = [
                "--from", "2026-10-01T10:00:00Z",
                "--to", "2026-10-01T12:00:00Z",
            ]
            self.assert_ok(run_find(audit_file, args), ["from", "mid", "to"])

    def test_from_only_and_to_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(
                audit_file,
                [
                    self.accepted("a1", recorded_at="2026-10-01T08:00:00Z"),
                    self.accepted("a2", recorded_at="2026-10-02T08:00:00Z"),
                ],
            )
            self.assert_ok(
                run_find(audit_file, ["--from", "2026-10-02T00:00:00Z"]),
                ["a2"],
            )
            self.assert_ok(
                run_find(audit_file, ["--to", "2026-10-01T23:59:59Z"]),
                ["a1"],
            )

    def test_unparseable_recorded_at_excluded_with_date_filter_included_without(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(
                audit_file,
                [
                    self.accepted("offset", recorded_at="2026-10-01T10:00:00+08:00"),
                    self.accepted("text", recorded_at="not-a-time"),
                    self.accepted("good", recorded_at="2026-10-01T10:00:00Z"),
                ],
            )
            # 无日期过滤：三条都命中（recorded_at 是字符串即可见）。
            self.assert_ok(run_find(audit_file), ["offset", "text", "good"])
            # 有日期过滤：时间不可解析的行不命中。
            self.assert_ok(
                run_find(audit_file, ["--from", "2026-01-01T00:00:00Z"]),
                ["good"],
            )

    # ---- 记录形态：跳过且不计 count ----------------------------------------

    def _one_skip_case(self, raw_line):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            with open(audit_file, "w", encoding="utf-8") as fh:
                fh.write(raw_line.rstrip("\n") + "\n")
                fh.write(
                    json.dumps(self.accepted("keep"), ensure_ascii=False) + "\n"
                )
            self.assert_ok(run_find(audit_file), ["keep"])

    def test_empty_audit_id_skipped(self):
        self._one_skip_case(
            json.dumps(
                {"request_id": "r", "actor": "a", "recorded_at": "t",
                 "decision": "accepted", "audit_id": ""},
                ensure_ascii=False,
            )
        )

    def test_missing_field_skipped(self):
        self._one_skip_case(
            json.dumps(
                {"request_id": "r", "actor": "a",
                 "decision": "accepted", "audit_id": "x"},
                ensure_ascii=False,
            )
        )

    def test_wrong_type_field_skipped(self):
        self._one_skip_case(
            json.dumps(
                {"request_id": 1, "actor": "a", "recorded_at": "t",
                 "decision": "accepted", "audit_id": "x"},
                ensure_ascii=False,
            )
        )
        self._one_skip_case(
            json.dumps(
                {"request_id": "r", "actor": "a", "recorded_at": 9,
                 "decision": "accepted", "audit_id": "x"},
                ensure_ascii=False,
            )
        )
        self._one_skip_case(
            json.dumps(
                {"request_id": "r", "actor": ["a"], "recorded_at": "t",
                 "decision": "accepted", "audit_id": "x"},
                ensure_ascii=False,
            )
        )
        self._one_skip_case(
            json.dumps(
                {"request_id": "r", "actor": "a", "recorded_at": "t",
                 "decision": 400, "audit_id": "x"},
                ensure_ascii=False,
            )
        )

    def test_null_audit_id_and_decision_skipped(self):
        self._one_skip_case(
            json.dumps(
                {"request_id": "r", "actor": "a", "recorded_at": "t",
                 "decision": "accepted", "audit_id": None},
                ensure_ascii=False,
            )
        )
        self._one_skip_case(
            json.dumps(
                {"request_id": "r", "actor": "a", "recorded_at": "t",
                 "decision": None, "audit_id": "x"},
                ensure_ascii=False,
            )
        )

    def test_null_context_rejected_record_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(audit_file, [self.rejected("r1")])
            body = self.assert_ok(
                run_find(audit_file, ["--decision", "rejected"]), ["r1"]
            )
            rec = body["records"][0]
            self.assertIsNone(rec["request_id"])
            self.assertIsNone(rec["actor"])
            self.assertIsNone(rec["recorded_at"])

    # ---- 输出字段：顺序与脱敏 ----------------------------------------------

    def test_field_order_preserved_and_sensitive_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            rec = {
                "request_id": "req-1",
                "actor": "alice",
                "recorded_at": "2026-10-01T10:00:00Z",
                "decision": "accepted",
                "audit_id": "a1",
                "resource": {"resourceType": "Patient", "id": "p1"},
                "term_maps": [{"system": "s"}],
                "message": "boom",
                "note": "kept",
            }
            self.seed(audit_file, [rec])
            body = self.assert_ok(run_find(audit_file), ["a1"])
            out = body["records"][0]
            self.assertEqual(
                list(out.keys()),
                ["request_id", "actor", "recorded_at", "decision",
                 "audit_id", "note"],
            )
            self.assertNotIn("resource", out)
            self.assertNotIn("term_maps", out)
            self.assertNotIn("message", out)
            self.assertEqual(out["note"], "kept")

    def test_chain_line_shows_audit_digest_and_bundle_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            rec = self.accepted(
                "a1",
                audit_digest="0" * 64,
                bundle_type="transaction",
                entry_total=1,
                entry_succeeded=1,
                entry_failed=0,
                status=200,
                entries=[{"index": 0, "phase": "completed", "status": 200}],
            )
            self.seed(audit_file, [rec])
            body = self.assert_ok(run_find(audit_file), ["a1"])
            out = body["records"][0]
            self.assertEqual(out["audit_digest"], "0" * 64)
            self.assertEqual(out["bundle_type"], "transaction")
            self.assertEqual(out["entries"][0]["index"], 0)
            self.assertNotIn("resource", out)

    # ---- AuditReadError -----------------------------------------------------

    def assert_read_error(self, proc):
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        err = json.loads(proc.stderr)
        self.assertEqual(err["error"]["type"], "AuditReadError")
        self.assertTrue(err["error"]["message"])
        self.assertEqual(proc.stderr.count("\n"), 1)

    def test_missing_file_is_read_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assert_read_error(
                run_find(os.path.join(tmp, "nope.jsonl"))
            )

    def test_directory_is_read_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assert_read_error(run_find(tmp))

    def test_missing_parent_is_read_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assert_read_error(
                run_find(os.path.join(tmp, "nodir", "x.jsonl"))
            )

    def test_missing_audit_file_arg_is_read_error(self):
        cmd = [sys.executable, GATEWAY, "--audit-find"]
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=30,
        )
        self.assert_read_error(proc)

    # ---- InputError ---------------------------------------------------------

    def assert_input_error(self, args):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(audit_file, [self.accepted("a1")])
            cmd = [sys.executable, GATEWAY, "--audit-find",
                   "--audit-file", audit_file] + args
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            err = json.loads(proc.stderr)
            self.assertEqual(err["error"]["type"], "InputError")
            self.assertTrue(err["error"]["message"])
            self.assertEqual(proc.stderr.count("\n"), 1)

    def test_bad_decision_is_input_error(self):
        self.assert_input_error(["--decision", "maybe"])
        self.assert_input_error(["--decision", ""])

    def test_bad_timestamp_is_input_error(self):
        self.assert_input_error(["--from", "2026-10-01"])
        self.assert_input_error(["--to", "2026-10-01T10:00:00"])
        self.assert_input_error(["--from", "2026-13-01T00:00:00Z"])

    def test_empty_filter_value_is_input_error(self):
        self.assert_input_error(["--request-id", ""])
        self.assert_input_error(["--actor", ""])

    def test_find_with_verify_audit_is_input_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(audit_file, [self.accepted("a1")])
            cmd = [
                sys.executable, GATEWAY, "--audit-find", "--verify-audit",
                "--audit-file", audit_file,
            ]
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "InputError"
            )

    def test_filters_without_find_are_input_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            base = [sys.executable, GATEWAY, "--audit-file", audit_file]
            for extra in (
                ["--request-id", "r"],
                ["--actor", "a"],
                ["--decision", "accepted"],
                ["--from", "2026-10-01T00:00:00Z"],
                ["--to", "2026-10-01T00:00:00Z"],
            ):
                proc = subprocess.run(
                    base + extra, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(
                    json.loads(proc.stderr)["error"]["type"], "InputError",
                    extra,
                )

    # ---- AuditVerificationError：行非合法 JSON 对象 ------------------------

    def assert_verification_error(self, content):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "bad.jsonl")
            with open(audit_file, "w", encoding="utf-8") as fh:
                fh.write(content)
            proc = run_find(audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            err = json.loads(proc.stderr)
            self.assertEqual(err["error"]["type"], "AuditVerificationError")
            self.assertEqual(proc.stderr.count("\n"), 1)

    def test_malformed_json_line_is_verification_error(self):
        self.assert_verification_error("{not json\n")

    def test_non_object_json_line_is_verification_error(self):
        self.assert_verification_error('["x"]\n')
        self.assert_verification_error('"a string"\n')
        self.assert_verification_error("null\n")

    # ---- 只读：不读 stdin、不写文件 ----------------------------------------

    def test_does_not_read_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(audit_file, [self.accepted("a1")])
            # 即使标准输入喂入数据，检索模式也忽略它。
            proc = run_find(audit_file, stdin=subprocess.PIPE)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["count"], 1)

    def test_find_does_not_modify_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(audit_file, [self.accepted("a1")])
            with open(audit_file, "rb") as fh:
                before = fh.read()
            run_find(audit_file, ["--actor", "alice"])
            run_find(audit_file, ["--decision", "accepted"])
            with open(audit_file, "rb") as fh:
                self.assertEqual(fh.read(), before)

    def test_find_on_missing_file_does_not_create_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "nope.jsonl")
            run_find(audit_file)
            self.assertFalse(os.path.exists(audit_file))

    # ---- 与其他开关组合：均不改变检索结果 ----------------------------------

    def test_audit_chain_skips_digest_verification_on_broken_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            # 用含全部五个必填字段的完整记录生成合法链式行，再篡改 actor
            # （不重算摘要 → 链断裂，但记录形态对检索仍可见）。
            full = self.accepted("a1", actor="alice")
            line, _ = make_chain_line(GENESIS_DIGEST, full)
            obj = json.loads(line)
            obj["actor"] = "tampered"
            write_lines(audit_file, [canonical(obj)])
            # 不带 --audit-chain：检索不校验摘要，照常读。
            self.assert_ok(run_find(audit_file), ["a1"])
            # 带 --audit-chain：仍跳过摘要校验，结果不变。
            self.assert_ok(
                run_find(audit_file, args=["--audit-chain"]), ["a1"]
            )
            # 对照：--verify-audit 判定同一文件确实断链。
            verify_proc = run_verify(audit_file)
            self.assertEqual(verify_proc.returncode, 2)
            self.assertEqual(
                json.loads(verify_proc.stderr)["error"]["type"],
                "AuditVerificationError",
            )

    def test_inert_flags_do_not_change_result_or_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(
                audit_file,
                [
                    self.accepted("a1", actor="alice"),
                    self.accepted("a2", actor="bob"),
                ],
            )
            with open(audit_file, "rb") as fh:
                before = fh.read()
            args = ["--audit-chain", "--audit-errors",
                    "--strict-types", "--check-references"]
            proc = run_find(audit_file, ["--actor", "alice"], args=args)
            self.assert_ok(proc, ["a1"])
            # 不触发拒绝审计追加，也不做严格类型/引用处理。
            with open(audit_file, "rb") as fh:
                self.assertEqual(fh.read(), before)

    def test_find_with_audit_errors_does_not_write_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            self.seed(audit_file, [self.accepted("a1")])
            with open(audit_file, "rb") as fh:
                before = fh.read()
            # 坏行在检索语义下是 AuditVerificationError；--audit-errors
            # 不得因此追加任何 rejected 记录。
            with open(audit_file, "a", encoding="utf-8") as fh:
                fh.write("null\n")
            proc = run_find(audit_file, args=["--audit-errors"])
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"],
                "AuditVerificationError",
            )
            # 文件只多出我们手写的坏行，未额外追加拒绝记录。
            with open(audit_file, "rb") as fh:
                self.assertEqual(fh.read(), before + b"null\n")

    # ---- 端到端：真实 accepted/rejected/chain 行均可检索 -------------------

    def test_finds_real_records_from_gateway_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            # accepted 链式行
            good = base_request()
            good["audit_context"] = {
                "request_id": "req-1", "actor": "dr-house",
                "recorded_at": "2026-10-03T10:00:00Z",
            }
            proc = run_chain(good, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # rejected 行（audit-errors + 非法 JSON）
            bad = run_audit_errors(
                None, audit_file, args=["--audit-chain"], raw="{not json"
            )
            self.assertEqual(bad.returncode, 2)
            # 链仍完整
            self.assertEqual(run_verify(audit_file).returncode, 0)

            # 检索 actor=dr-house 命中 accepted 行，且带 audit_digest。
            found = run_find(audit_file, ["--actor", "dr-house"])
            self.assertEqual(found.returncode, 0, found.stderr)
            body = json.loads(found.stdout)
            self.assertEqual(body["count"], 1)
            rec = body["records"][0]
            self.assertEqual(rec["decision"], "accepted")
            self.assertRegex(rec["audit_digest"], r"^[0-9a-f]{64}$")
            self.assertNotIn("resource", rec)

            # rejected 行（request_id/actor 为 null）按 decision 命中。
            rej = run_find(audit_file, ["--decision", "rejected"])
            rej_body = json.loads(rej.stdout)
            self.assertEqual(rej_body["count"], 1)
            self.assertIsNone(rej_body["records"][0]["actor"])


class ReferenceCheckTests(unittest.TestCase):
    def assert_ref_error(self, proc, audit_file, value=None):
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertFalse(os.path.exists(audit_file))
        err = json.loads(proc.stderr)
        self.assertEqual(err["error"]["type"], "FhirValidationError")
        self.assertTrue(err["error"]["message"])
        # stderr 恰好一行 JSON；message 含引用值或其形态描述。
        self.assertEqual(proc.stderr.count("\n"), 1)
        if value is not None:
            self.assertIn(value, err["error"]["message"])
        return err["error"]["message"]

    def _patient(self, reference=None, **extra):
        resource = {"resourceType": "Patient", "id": "pat-1"}
        if reference is not None:
            resource["managingOrganization"] = {"reference": reference}
        resource.update(extra)
        return resource

    def _observation(self, reference=None, oid="obs-1"):
        resource = {
            "resourceType": "Observation",
            "id": oid,
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
        }
        if reference is not None:
            resource["subject"] = {"reference": reference}
        return resource

    # ---- 单资源 ----------------------------------------------------------

    def test_single_contained_fragment_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = self._patient(
                reference="#rp-1",
                contained=[{"resourceType": "RelatedPerson", "id": "rp-1"}],
            )
            proc = run_check(base_request(resource=resource, term_maps=[]), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(
                list(out.keys()),
                ["status", "resource_type", "resource", "mappings", "audit"],
            )
            self.assertEqual(out["resource"], resource)
            self.assertEqual(out["mappings"], [])
            self.assertEqual(
                set(out["audit"]),
                {"request_id", "actor", "recorded_at", "decision", "audit_id"},
            )
            with open(audit_file, encoding="utf-8") as fh:
                raw = fh.read()
            self.assertNotIn("#rp-1", raw)

    def test_single_contained_fragment_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = self._patient(
                reference="#ghost",
                contained=[{"resourceType": "RelatedPerson", "id": "rp-1"}],
            )
            proc = run_check(base_request(resource=resource, term_maps=[]), audit_file)
            self.assert_ref_error(proc, audit_file, "#ghost")

    def test_fragment_cannot_hit_top_level_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            # #pat-1 只能命中同资源 contained，不能命中资源自身的顶层 id。
            resource = self._patient(reference="#pat-1")
            proc = run_check(base_request(resource=resource, term_maps=[]), audit_file)
            self.assert_ref_error(proc, audit_file, "#pat-1")

    def test_single_relative_reference_is_external_format_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = self._patient(reference="Organization/org-1")
            proc = run_check(base_request(resource=resource, term_maps=[]), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_single_absolute_urls_are_external(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            for url in (
                "http://example.com/fhir/Patient/p1",
                "https://example.com/fhir/Patient/p1",
                "http://x",
            ):
                resource = self._patient(reference=url)
                proc = run_check(
                    base_request(resource=resource, term_maps=[]), audit_file
                )
                self.assertEqual(proc.returncode, 0, (url, proc.stderr))

    def test_reference_without_key_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = self._patient()
            resource["managingOrganization"] = {"display": "某机构"}
            proc = run_check(base_request(resource=resource, term_maps=[]), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_nested_reference_is_traversed(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = {
                "resourceType": "Patient",
                "id": "pat-1",
                "contact": [
                    {
                        "name": {"text": "x"},
                        "extension": [
                            {"url": "e", "valueReference": {"reference": "Patient/x"}}
                        ],
                    }
                ],
            }
            # 单资源：相对引用仅校验格式，"Patient/x" 合法。
            proc = run_check(base_request(resource=resource, term_maps=[]), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(audit_file, encoding="utf-8") as fh:
                lines_before = fh.read()
            self.assertEqual(lines_before.count("\n"), 1)

            resource["contact"][0]["extension"][0]["valueReference"]["reference"] = 123
            proc = run_check(base_request(resource=resource, term_maps=[]), audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            # 失败不追加审计行。
            with open(audit_file, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), lines_before)

    def test_bad_reference_forms_rejected(self):
        cases = [
            ("Patient", "Patient"),
            ("Patient/", None),
            ("/pat-1", None),
            ("Patient/p1/extra", None),
            ("#", "#"),
            ("urn:uuid:abc", "urn:uuid:abc"),
            ("Patient p1", None),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            for value, fragment in cases:
                resource = self._patient(reference=value)
                proc = run_check(
                    base_request(resource=resource, term_maps=[]), audit_file
                )
                message = self.assert_ref_error(
                    proc, audit_file, fragment if fragment is not None else value
                )
                # 每条失败都有唯一原因，且不写审计。
                self.assertTrue(message)

    def test_non_string_reference_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            for bad in (123, 1.5, True, None, ["Patient/p1"], {"reference": "x"}):
                resource = self._patient()
                resource["managingOrganization"] = {"reference": bad}
                proc = run_check(
                    base_request(resource=resource, term_maps=[]), audit_file
                )
                self.assert_ref_error(proc, audit_file)

    def test_empty_string_reference_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = self._patient(reference="")
            proc = run_check(base_request(resource=resource, term_maps=[]), audit_file)
            self.assert_ref_error(proc, audit_file, '""')

    def test_without_flag_references_not_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            # 无 --check-references：悬空片段、非法形式、非字符串全部放行。
            for bad in ("#ghost", "not-a-ref", 123, ""):
                resource = self._patient()
                resource["managingOrganization"] = {"reference": bad}
                proc = run(base_request(resource=resource, term_maps=[]), audit_file)
                self.assertEqual(proc.returncode, 0, (bad, proc.stderr))

    # ---- 非执行 Bundle ----------------------------------------------------

    def _ref_bundle(self, ref, btype="collection"):
        return bundle_request(
            [
                patient_entry("pat-1"),
                {
                    "resource": {
                        "resourceType": "Observation",
                        "id": "obs-1",
                        "status": "final",
                        "code": {
                            "coding": [{"system": "http://loinc.org", "code": "8867-4"}]
                        },
                        "subject": {"reference": ref},
                    }
                },
            ],
            bundle_type=btype,
        )

    def test_bundle_cross_entry_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run_check(self._ref_bundle("Patient/pat-1"), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["resource_type"], "Bundle")
            self.assertEqual(
                list(out.keys()),
                ["status", "resource_type", "resource", "mappings", "audit"],
            )
            with open(audit_file, encoding="utf-8") as fh:
                self.assertEqual(len(fh.readlines()), 1)

    def test_bundle_cross_entry_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run_check(self._ref_bundle("Patient/ghost"), audit_file)
            self.assert_ref_error(proc, audit_file, "Patient/ghost")

    def test_bundle_duplicate_target_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = self._ref_bundle("Patient/pat-1")
            req["resource"]["entry"].append(patient_entry("pat-1"))
            proc = run_check(req, audit_file)
            message = self.assert_ref_error(proc, audit_file, "Patient/pat-1")
            self.assertIn("不唯一", message)

    def test_bundle_type_and_id_must_both_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            # 存在 Patient/pat-1 与 Observation/obs-1，Observation/pat-1 不命中。
            proc = run_check(self._ref_bundle("Observation/pat-1"), audit_file)
            self.assert_ref_error(proc, audit_file, "Observation/pat-1")

    def test_bundle_fragment_scoped_to_entry_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            # 其他 entry 的 Patient/pat-1 不能作为 #pat-1 命中。
            proc = run_check(self._ref_bundle("#pat-1"), audit_file)
            self.assert_ref_error(proc, audit_file, "#pat-1")

            # 同 entry.resource.contained 内命中即可。
            req = self._ref_bundle("#inline")
            req["resource"]["entry"][1]["resource"]["contained"] = [
                {"resourceType": "Patient", "id": "inline"}
            ]
            proc = run_check(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_bundle_absolute_url_is_external(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run_check(
                self._ref_bundle("https://example.com/fhir/Patient/x"), audit_file
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_bundle_malformed_reference_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            for ref in ("Patient", "Patient/", "#", "urn:uuid:1", 7):
                proc = run_check(self._ref_bundle(ref), audit_file)
                self.assert_ref_error(proc, audit_file)

    def test_bundle_level_reference_checked_against_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request([patient_entry("pat-1")])
            req["resource"]["link"] = [
                {"extension": [{"valueReference": {"reference": "Patient/zzz"}}]}
            ]
            proc = run_check(req, audit_file)
            self.assert_ref_error(proc, audit_file, "Patient/zzz")

    def test_bundle_non_executable_types_all_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            for btype in (
                "document",
                "message",
                "transaction-response",
                "batch-response",
                "history",
                "searchset",
                "collection",
            ):
                req = self._ref_bundle("Patient/ghost", btype=btype)
                proc = run_check(req, audit_file)
                self.assertEqual(proc.returncode, 2, (btype, proc.stderr))
                err = json.loads(proc.stderr)
                self.assertEqual(err["error"]["type"], "FhirValidationError")

    # ---- transaction/batch 完全旁路 --------------------------------------

    def test_executable_bundles_skip_reference_checks(self):
        for btype in ("transaction", "batch"):
            with tempfile.TemporaryDirectory() as tmp:
                audit_file = os.path.join(tmp, "audit-%s.jsonl" % btype)
                entries = [
                    tx_entry(
                        {
                            "resourceType": "Observation",
                            "id": "obs-1",
                            "status": "final",
                            "code": {
                                "coding": [
                                    {"system": "http://loinc.org", "code": "8867-4"}
                                ]
                            },
                            "subject": {"reference": "Patient/ghost"},
                        }
                    )
                ]
                proc = run_check(executable_request(entries, btype), audit_file)
                self.assertEqual(proc.returncode, 0, (btype, proc.stderr))
                out = json.loads(proc.stdout)
                self.assertEqual(out["status"], 200)
                self.assertEqual(out["resource"]["entry"][0]["status"], 200)
                # 审计照常写入。
                self.assertTrue(os.path.exists(audit_file))

    def test_executable_bundle_failure_semantics_unchanged_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(patient_resource("pat-1")),
                tx_entry(patient_resource("pat-2"), method="DELETE"),
            ]
            # transaction：全有或全无；batch：逐项独立。
            proc = run_check(executable_request(entries, "transaction"), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["status"], 400)
            proc = run_check(executable_request(entries, "batch"), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["status"], 200)

    # ---- 优先级与组合 ----------------------------------------------------

    def test_input_error_precedes_reference_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(resource=self._patient("#ghost"), term_maps=[])
            req["audit_context"] = {"request_id": "", "actor": "a", "recorded_at": "t"}
            proc = run_check(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "InputError"
            )
            self.assertFalse(os.path.exists(audit_file))

    def test_resource_validation_precedes_reference_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            # Observation.status 非法 + 悬空引用：先报资源校验错误。
            resource = self._observation("Patient/ghost")
            resource["status"] = "draft"
            proc = run_check(
                base_request(resource=resource, term_maps=[]), audit_file
            )
            self.assertEqual(proc.returncode, 2)
            err = json.loads(proc.stderr)
            self.assertEqual(err["error"]["type"], "FhirValidationError")
            self.assertIn("status", err["error"]["message"])
            self.assertFalse(os.path.exists(audit_file))

    def test_reference_error_precedes_term_mapping_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(resource=self._patient("#ghost"))
            req["term_maps"] = [
                {"system": "", "code": "c", "target_system": "ts", "target_code": "tc"}
            ]
            proc = run_check(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "FhirValidationError"
            )
            self.assertFalse(os.path.exists(audit_file))

            # 不带选项时同一请求落到术语映射错误。
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "TermMappingError"
            )

    def test_check_references_with_audit_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = self._patient(
                reference="#rp-1",
                contained=[{"resourceType": "RelatedPerson", "id": "rp-1"}],
            )
            req = base_request(resource=resource, term_maps=[])
            proc = run(req, audit_file, args=["--check-references", "--audit-chain"])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertRegex(out["audit"]["audit_digest"], r"^[0-9a-f]{64}$")
            # 字段不新增（仅链式模式既有的 audit_digest），引用值不入审计。
            with open(audit_file, encoding="utf-8") as fh:
                raw = fh.read()
            self.assertNotIn("#rp-1", raw)
            self.assertEqual(
                run_verify(audit_file).returncode, 0
            )

    def test_check_references_does_not_affect_verify_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run_chain(base_request(), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = subprocess.run(
                [
                    sys.executable,
                    GATEWAY,
                    "--check-references",
                    "--verify-audit",
                    "--audit-file",
                    audit_file,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["line_count"], 1)


AUDIT_ERRORS_ARGS = ["--audit-errors"]


def run_audit_errors(payload, audit_file, args=None, raw=None):
    return run(payload, audit_file, args=AUDIT_ERRORS_ARGS + (args or []), raw=raw)


def read_audit_lines(audit_file):
    with open(audit_file, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh.read().splitlines()]


class AuditErrorsTests(unittest.TestCase):
    REJECTION_FIELDS = {
        "request_id",
        "actor",
        "recorded_at",
        "decision",
        "audit_id",
        "error_type",
        "phase",
        "status",
    }

    def assert_rejection(self, record, error_type, phase, context=None):
        self.assertEqual(set(record), self.REJECTION_FIELDS)
        self.assertEqual(record["decision"], "rejected")
        self.assertEqual(record["status"], 400)
        self.assertEqual(record["error_type"], error_type)
        self.assertEqual(record["phase"], phase)
        self.assertTrue(record["audit_id"])
        if context is None:
            self.assertIsNone(record["request_id"])
            self.assertIsNone(record["actor"])
            self.assertIsNone(record["recorded_at"])
        else:
            self.assertEqual(record["request_id"], context["request_id"])
            self.assertEqual(record["actor"], context["actor"])
            self.assertEqual(record["recorded_at"], context["recorded_at"])

    def test_invalid_json_rejection_with_null_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run_audit_errors(None, audit_file, raw="not json")
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            err = json.loads(proc.stderr)["error"]
            self.assertEqual(err["type"], "InputError")
            (record,) = read_audit_lines(audit_file)
            self.assert_rejection(record, "InputError", "request")

    def test_request_structure_failure_keeps_valid_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request()
            del req["resource"]
            proc = run_audit_errors(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            (record,) = read_audit_lines(audit_file)
            self.assert_rejection(
                record, "InputError", "request", context=req["audit_context"]
            )

    def test_audit_context_failure_nulls_trio(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(
                audit_context={"request_id": "r", "actor": "", "recorded_at": "t"}
            )
            proc = run_audit_errors(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            (record,) = read_audit_lines(audit_file)
            self.assert_rejection(record, "InputError", "request")

    def test_validation_failure_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(
                resource={"resourceType": "Observation", "id": "o1", "status": "bogus"}
            )
            proc = run_audit_errors(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            err = json.loads(proc.stderr)["error"]
            self.assertEqual(err["type"], "FhirValidationError")
            (record,) = read_audit_lines(audit_file)
            self.assert_rejection(
                record, "FhirValidationError", "validation",
                context=req["audit_context"],
            )

    def test_reference_failure_is_validation_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = {
                "resourceType": "Observation",
                "id": "o1",
                "status": "final",
                "code": {"coding": [{"system": "s", "code": "c"}]},
                "subject": {"reference": "#missing"},
            }
            req = base_request(resource=resource, term_maps=[])
            proc = run_audit_errors(req, audit_file, args=["--check-references"])
            self.assertEqual(proc.returncode, 2)
            (record,) = read_audit_lines(audit_file)
            self.assert_rejection(
                record, "FhirValidationError", "validation",
                context=req["audit_context"],
            )

    def test_mapping_failures_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            bad_item = base_request(term_maps=[{"system": "s"}])
            proc = run_audit_errors(bad_item, audit_file)
            self.assertEqual(proc.returncode, 2)
            conflict = base_request(
                term_maps=[
                    {"system": "s", "code": "c",
                     "target_system": "t", "target_code": "1"},
                    {"system": "s", "code": "c",
                     "target_system": "t", "target_code": "2"},
                ]
            )
            proc = run_audit_errors(conflict, audit_file)
            self.assertEqual(proc.returncode, 2)
            records = read_audit_lines(audit_file)
            self.assertEqual(len(records), 2)
            for record in records:
                self.assert_rejection(
                    record, "TermMappingError", "mapping",
                    context=conflict["audit_context"],
                )
            self.assertNotEqual(records[0]["audit_id"], records[1]["audit_id"])

    def test_rejection_record_privacy_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(
                resource={"resourceType": "Patient", "name": "secret-patient"}
            )
            proc = run_audit_errors(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            with open(audit_file, encoding="utf-8") as fh:
                raw = fh.read()
            # 不写入 resource、term_maps、引用、message 或正文内容。
            self.assertNotIn("secret-patient", raw)
            self.assertNotIn("resource", raw)
            self.assertNotIn("term_maps", raw)
            self.assertNotIn("message", raw)
            self.assertNotIn("resource.id", raw)

    def test_flag_off_failure_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run(base_request(resource={"resourceType": "Patient"}), audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(os.path.exists(audit_file))

    def test_success_path_unchanged_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run_audit_errors(base_request(), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], "accepted")
            (record,) = read_audit_lines(audit_file)
            self.assertEqual(record["decision"], "accepted")
            self.assertNotIn("error_type", record)
            self.assertNotIn("phase", record)

    def test_transaction_entry_failure_adds_no_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(
                resource={
                    "resourceType": "Bundle",
                    "id": "b1",
                    "type": "transaction",
                    "entry": [
                        {
                            "request": {"method": "DELETE", "url": "Patient/p1"},
                            "resource": {"resourceType": "Patient", "id": "p1"},
                        }
                    ],
                },
                term_maps=[],
            )
            proc = run_audit_errors(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 400)
            (record,) = read_audit_lines(audit_file)
            self.assertEqual(record["decision"], "accepted")
            self.assertEqual(record["entry_failed"], 1)

    def test_chained_rejection_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            ok = run_audit_errors(base_request(), audit_file, args=["--audit-chain"])
            self.assertEqual(ok.returncode, 0, ok.stderr)
            bad = run_audit_errors(
                base_request(resource={"resourceType": "Patient"}),
                audit_file,
                args=["--audit-chain"],
            )
            self.assertEqual(bad.returncode, 2)
            lines = read_audit_lines(audit_file)
            self.assertEqual(len(lines), 2)
            rejection = lines[1]
            self.assertEqual(rejection["decision"], "rejected")
            body = {k: v for k, v in rejection.items() if k != "audit_digest"}
            self.assertEqual(
                rejection["audit_digest"],
                chain_digest(lines[0]["audit_digest"], body),
            )
            proc = run_verify(audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["line_count"], 2)

    def test_broken_chain_rejection_is_write_error_file_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            write_lines(audit_file, ['{"plain": true}'])
            proc = run_audit_errors(
                base_request(resource={"resourceType": "Patient"}),
                audit_file,
                args=["--audit-chain"],
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            err = json.loads(proc.stderr)["error"]
            self.assertEqual(err["type"], "AuditWriteError")
            with open(audit_file, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), '{"plain": true}\n')

    def test_unwritable_audit_target_is_write_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "missing-dir", "audit.jsonl")
            proc = run_audit_errors(
                base_request(resource={"resourceType": "Patient"}), audit_file
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            err = json.loads(proc.stderr)["error"]
            self.assertEqual(err["type"], "AuditWriteError")
            self.assertFalse(os.path.exists(audit_file))

    def test_verify_audit_with_flag_is_input_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            result = subprocess.run(
                [
                    sys.executable,
                    GATEWAY,
                    "--verify-audit",
                    "--audit-errors",
                    "--audit-file",
                    audit_file,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            err = json.loads(result.stderr)["error"]
            self.assertEqual(err["type"], "InputError")
            self.assertFalse(os.path.exists(audit_file))

    def test_missing_audit_file_arg_still_write_error(self):
        proc = run(base_request(), args=AUDIT_ERRORS_ARGS)
        self.assertEqual(proc.returncode, 2)
        err = json.loads(proc.stderr)["error"]
        self.assertEqual(err["type"], "AuditWriteError")


STRICT_ARGS = ["--strict-types"]


def run_strict(payload, audit_file, args=None, raw=None):
    return run(payload, audit_file, args=STRICT_ARGS + (args or []), raw=raw)


def strict_patient(**overrides):
    resource = {"resourceType": "Patient", "id": "p1"}
    resource.update(overrides)
    return resource


def strict_observation(**overrides):
    resource = {
        "resourceType": "Observation",
        "id": "o1",
        "status": "final",
        "code": {"coding": [{"system": "s", "code": "c"}]},
    }
    resource.update(overrides)
    return resource


def strict_condition(**overrides):
    resource = {
        "resourceType": "Condition",
        "id": "c1",
        "clinicalStatus": {"coding": [{"system": "s", "code": "active"}]},
    }
    resource.update(overrides)
    return resource


class StrictTypesSuccessTests(unittest.TestCase):
    def test_valid_patient_full_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = strict_patient(
                active=True,
                gender="female",
                birthDate="1980-02-29",
                name=[{"family": "Doe", "given": ["Jane"], "text": "Jane Doe"}],
                telecom=[{"system": "phone", "value": "123", "rank": 1}],
            )
            proc = run_strict(base_request(resource=resource, term_maps=[]), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["resource"], resource)
            self.assertEqual(
                list(out.keys()),
                ["status", "resource_type", "resource", "mappings", "audit"],
            )

    def test_valid_observation_value_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            for resource in (
                strict_observation(valueQuantity={"value": 1.5, "unit": "kg"}),
                strict_observation(valueInteger=42),
                strict_observation(valueString="normal"),
                strict_observation(valueBoolean=True),
                strict_observation(
                    valueCodeableConcept={
                        "coding": [
                            {
                                "system": "s",
                                "code": "c",
                                "display": "C",
                                "userSelected": False,
                            }
                        ],
                        "text": "concept",
                    }
                ),
            ):
                proc = run_strict(
                    base_request(resource=resource, term_maps=[]), audit_file
                )
                self.assertEqual(proc.returncode, 0, proc.stderr + json.dumps(resource))

    def test_valid_observation_datetimes_and_condition(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            obs = strict_observation(
                effectiveDateTime="2026-10-03T10:00:00Z",
                issued="2026-10-03T18:30:00.123+08:00",
            )
            self.assertEqual(
                run_strict(base_request(resource=obs, term_maps=[]), audit_file).returncode,
                0,
            )
            cond = strict_condition(
                verificationStatus={"coding": [{"system": "s", "code": "confirmed"}]},
                code={"coding": [{"system": "s", "code": "x"}]},
                onsetDateTime="2026-10-03T10:00:00Z",
                recordedDate="2026-10-03",
            )
            proc = run_strict(base_request(resource=cond, term_maps=[]), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_unknown_fields_and_name_telecom_internals_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = strict_patient(
                weird={"nested": [1, True, None]},
                name=[{"family": 1, "given": "x", "unknown_field": {"k": 0}}],
                telecom=[{"value": 1}],
            )
            proc = run_strict(base_request(resource=resource, term_maps=[]), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["resource"], resource)

    def test_optional_codeable_concept_coding_may_be_empty_or_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            cond = strict_condition(
                code={"coding": [], "text": "free text"},
                verificationStatus={"text": "confirmed"},
            )
            proc = run_strict(base_request(resource=cond, term_maps=[]), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_flag_off_keeps_baseline_permissive_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            for resource in (
                strict_patient(active=1, gender="X", birthDate=1980, name={}),
                strict_observation(valueInteger="5", valueString=1),
                strict_condition(verificationStatus="not-an-object"),
            ):
                proc = run(base_request(resource=resource, term_maps=[]), audit_file)
                self.assertEqual(proc.returncode, 0, proc.stderr + json.dumps(resource))

    def test_non_executable_bundle_strict_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request(
                [
                    {"resource": strict_patient(active=True)},
                    {"resource": strict_observation(valueInteger=3)},
                    {"resource": strict_condition(recordedDate="2026-10-03")},
                ]
            )
            proc = run_strict(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["resource_type"], "Bundle")

    def test_transaction_and_batch_strict_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(strict_patient(active=True)),
                tx_entry(strict_observation(effectiveDateTime="2026-10-03T10:00:00Z")),
            ]
            for bundle_type in ("transaction", "batch"):
                proc = run_strict(
                    executable_request(entries, bundle_type=bundle_type), audit_file
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                out = json.loads(proc.stdout)
                self.assertEqual(out["status"], 200)
                self.assertEqual(
                    [e["status"] for e in out["resource"]["entry"]], [200, 200]
                )


class StrictTypesPatientFailureTests(unittest.TestCase):
    def assert_fhir_error(self, proc, *parts):
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        err = json.loads(proc.stderr)["error"]
        self.assertEqual(err["type"], "FhirValidationError")
        message = err["message"]
        for part in parts:
            self.assertIn(part, message)
        return message

    def strict_fail(self, resource, *parts, args=None):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run_strict(
                base_request(resource=resource, term_maps=[]),
                audit_file,
                args=args,
            )
            self.assertFalse(os.path.exists(audit_file))
            return self.assert_fhir_error(proc, *parts)

    def test_active_wrong_types(self):
        self.strict_fail(strict_patient(active=1), "Patient.active", "布尔")
        self.strict_fail(strict_patient(active="true"), "Patient.active", "布尔")
        self.strict_fail(strict_patient(active=None), "Patient.active", "null")

    def test_gender_invalid(self):
        self.strict_fail(strict_patient(gender="MALE"), "Patient.gender")
        self.strict_fail(strict_patient(gender=1), "Patient.gender")
        self.strict_fail(strict_patient(gender=None), "Patient.gender")

    def test_birth_date_invalid(self):
        self.strict_fail(strict_patient(birthDate=19800229), "Patient.birthDate", "字符串")
        self.strict_fail(strict_patient(birthDate="1980-13-01"), "Patient.birthDate")
        self.strict_fail(strict_patient(birthDate="1980/02/29"), "Patient.birthDate")
        self.strict_fail(strict_patient(birthDate="1981-02-29"), "Patient.birthDate")
        self.strict_fail(strict_patient(birthDate=None), "Patient.birthDate", "null")

    def test_name_and_telecom_shape(self):
        self.strict_fail(strict_patient(name={"family": "Doe"}), "Patient.name", "数组")
        self.strict_fail(strict_patient(name=["Doe"]), "Patient.name[0]", "对象")
        self.strict_fail(
            strict_patient(telecom="phone"),
            "Patient.telecom",
            "数组",
        )
        self.strict_fail(
            strict_patient(telecom=[1]), "Patient.telecom[0]", "对象"
        )


class StrictTypesObservationFailureTests(unittest.TestCase):
    def strict_fail(self, resource, *parts):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run_strict(
                base_request(resource=resource, term_maps=[]), audit_file
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            self.assertFalse(os.path.exists(audit_file))
            message = json.loads(proc.stderr)["error"]["message"]
            for part in parts:
                self.assertIn(part, message)
            return message

    def test_codeable_concept_shape(self):
        # 首个 coding 的 system/code 类型错误先被基线 _primary_coding 拦截；
        # 严格收紧对后续 coding 元素与 display/userSelected/text 生效。
        self.strict_fail(
            strict_observation(code={"coding": "x"}), "Observation.code.coding", "数组"
        )
        self.strict_fail(
            strict_observation(code={"coding": ["x"]}),
            "Observation.code.coding[0]",
            "对象",
        )
        self.strict_fail(
            strict_observation(
                code={
                    "coding": [
                        {"system": "s", "code": "c"},
                        {"system": 1, "code": "c2"},
                    ]
                }
            ),
            "Observation.code.coding[1].system",
            "字符串",
        )
        self.strict_fail(
            strict_observation(
                code={"coding": [{"system": "s", "code": "c", "display": 5}]}
            ),
            "Observation.code.coding[0].display",
            "字符串",
        )
        self.strict_fail(
            strict_observation(
                code={
                    "coding": [{"system": "s", "code": "c", "userSelected": "yes"}]
                }
            ),
            "Observation.code.coding[0].userSelected",
            "布尔",
        )
        self.strict_fail(
            strict_observation(code={"coding": [{"system": "s", "code": "c"}], "text": 9}),
            "Observation.code.text",
            "字符串",
        )

    def test_value_choices_wrong_types(self):
        self.strict_fail(
            strict_observation(valueQuantity="5"), "Observation.valueQuantity", "对象"
        )
        self.strict_fail(
            strict_observation(valueQuantity=None), "Observation.valueQuantity", "null"
        )
        self.strict_fail(
            strict_observation(valueInteger=True), "Observation.valueInteger", "整数"
        )
        self.strict_fail(
            strict_observation(valueInteger=1.5), "Observation.valueInteger", "整数"
        )
        self.strict_fail(
            strict_observation(valueInteger="5"), "Observation.valueInteger", "整数"
        )
        self.strict_fail(
            strict_observation(valueString=5), "Observation.valueString", "字符串"
        )
        self.strict_fail(
            strict_observation(valueBoolean=0), "Observation.valueBoolean", "布尔"
        )
        self.strict_fail(
            strict_observation(valueBoolean="true"),
            "Observation.valueBoolean",
            "布尔",
        )

    def test_multiple_value_choices_rejected(self):
        message = self.strict_fail(
            strict_observation(valueString="x", valueQuantity={"value": 1}),
            "value[x]",
        )
        self.assertIn("valueString", message)
        self.assertIn("valueQuantity", message)

    def test_value_codeable_concept_is_part_of_choice(self):
        message = self.strict_fail(
            strict_observation(
                valueCodeableConcept={"coding": [{"system": "s", "code": "c"}]},
                valueBoolean=True,
            ),
            "value[x]",
        )
        self.assertIn("valueCodeableConcept", message)
        self.assertIn("valueBoolean", message)

    def test_value_codeable_concept_shape(self):
        self.strict_fail(
            strict_observation(valueCodeableConcept="x"),
            "Observation.valueCodeableConcept",
            "对象",
        )
        self.strict_fail(
            strict_observation(
                valueCodeableConcept={"coding": [{"system": "s", "code": None}]}
            ),
            "Observation.valueCodeableConcept.coding[0].code",
        )
        self.strict_fail(
            strict_observation(
                valueCodeableConcept={
                    "coding": [{"system": "s", "code": "c", "userSelected": 1}]
                }
            ),
            "Observation.valueCodeableConcept.coding[0].userSelected",
            "布尔",
        )

    def test_datetimes_invalid(self):
        self.strict_fail(
            strict_observation(effectiveDateTime="2026-10-03"),
            "Observation.effectiveDateTime",
        )
        self.strict_fail(
            strict_observation(effectiveDateTime="not-a-date"),
            "Observation.effectiveDateTime",
        )
        self.strict_fail(
            strict_observation(effectiveDateTime="2026-02-30T00:00:00Z"),
            "Observation.effectiveDateTime",
        )
        self.strict_fail(
            strict_observation(effectiveDateTime=20261003),
            "Observation.effectiveDateTime",
            "字符串",
        )
        self.strict_fail(strict_observation(issued=123), "Observation.issued", "字符串")
        self.strict_fail(
            strict_observation(issued="2026-10-03"), "Observation.issued"
        )


class StrictTypesConditionFailureTests(unittest.TestCase):
    def strict_fail(self, resource, *parts):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run_strict(
                base_request(resource=resource, term_maps=[]), audit_file
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            message = json.loads(proc.stderr)["error"]["message"]
            for part in parts:
                self.assertIn(part, message)

    def test_codeable_concepts(self):
        self.strict_fail(
            strict_condition(verificationStatus="confirmed"),
            "Condition.verificationStatus",
            "对象",
        )
        self.strict_fail(
            strict_condition(code=[]), "Condition.code", "对象"
        )
        self.strict_fail(
            strict_condition(
                verificationStatus={"coding": [{"system": "s", "code": True}]}
            ),
            "Condition.verificationStatus.coding[0].code",
            "字符串",
        )

    def test_onset_and_recorded_dates(self):
        self.strict_fail(
            strict_condition(onsetDateTime=5), "Condition.onsetDateTime", "字符串"
        )
        self.strict_fail(
            strict_condition(onsetDateTime="2026-10-03"), "Condition.onsetDateTime"
        )
        self.strict_fail(
            strict_condition(recordedDate="2026-10-03T00:00:00Z"),
            "Condition.recordedDate",
        )
        self.strict_fail(
            strict_condition(recordedDate="2026-02-30"), "Condition.recordedDate"
        )


class StrictTypesBundleFailureTests(unittest.TestCase):
    def test_non_executable_bundle_entry_failure_has_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request(
                [
                    {"resource": strict_patient()},
                    {"resource": strict_observation(valueInteger="bad")},
                ]
            )
            proc = run_strict(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            message = json.loads(proc.stderr)["error"]["message"]
            self.assertIn("Bundle.entry[1].resource.valueInteger", message)
            self.assertFalse(os.path.exists(audit_file))

    def test_transaction_strict_failure_is_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(strict_patient()),
                tx_entry(strict_observation(valueQuantity=5)),
            ]
            proc = run_strict(executable_request(entries), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 400)
            self.assertEqual(
                [e["status"] for e in out["resource"]["entry"]], [200, 400]
            )
            issue = out["resource"]["entry"][1]["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "processing")
            self.assertEqual(issue["expression"], "Bundle.entry[1].resource")
            self.assertIn("valueQuantity", issue["diagnostics"])
            audit_entry = out["audit"]["entries"][1]
            self.assertEqual(audit_entry["phase"], "validation")
            self.assertEqual(audit_entry["issue_code"], "processing")

    def test_batch_strict_failure_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(strict_patient(active="yes")),
                tx_entry(strict_patient()),
            ]
            proc = run_strict(
                executable_request(entries, bundle_type="batch"), audit_file
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 200)
            self.assertEqual(
                [e["status"] for e in out["resource"]["entry"]], [400, 200]
            )
            self.assertEqual(
                out["resource"]["entry"][0]["outcome"]["issue"][0]["code"],
                "processing",
            )

    def test_request_invalid_precedes_strict_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                {
                    "request": {"method": "POST", "url": "Observation/o1"},
                    "resource": strict_patient(active=1),
                }
            ]
            proc = run_strict(executable_request(entries), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            issue = json.loads(proc.stdout)["resource"]["entry"][0]["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "invalid")

    def test_strict_validation_precedes_mapping_in_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = executable_request([tx_entry(strict_observation(valueInteger="x"))])
            req["term_maps"] = [{"system": "s"}]
            proc = run_strict(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            audit_entry = out["audit"]["entries"][0]
            self.assertEqual(audit_entry["phase"], "validation")
            self.assertEqual(audit_entry["issue_code"], "processing")

    def test_flag_off_bundle_keeps_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request([{"resource": strict_patient(active=1)}])
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)


class StrictTypesPrecedenceAndComboTests(unittest.TestCase):
    def test_type_check_precedes_reference_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = strict_patient(
                active="yes",
                generalPractitioner={"reference": "#missing"},
            )
            req = base_request(resource=resource, term_maps=[])
            proc = run_strict(req, audit_file, args=["--check-references"])
            self.assertEqual(proc.returncode, 2)
            message = json.loads(proc.stderr)["error"]["message"]
            self.assertIn("Patient.active", message)

    def test_reference_check_precedes_term_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = strict_observation(
                subject={"reference": "#missing"},
            )
            req = base_request(
                resource=resource, term_maps=[{"system": "s"}]
            )
            proc = run_strict(req, audit_file, args=["--check-references"])
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "FhirValidationError"
            )

    def test_input_error_precedes_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            # audit_context 非法（InputError）先于资源严格失败、引用失败与
            # term_maps 的 TermMappingError，即使所有开关同开。
            req = base_request(
                resource=strict_patient(
                    active="bad",
                    generalPractitioner={"reference": "#missing"},
                ),
                term_maps=[{"system": "s"}],
            )
            req["audit_context"] = {"request_id": "r", "actor": "", "recorded_at": "t"}
            proc = run_strict(req, audit_file, args=["--check-references"])
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "InputError"
            )
            self.assertFalse(os.path.exists(audit_file))

    def test_audit_errors_writes_rejected_then_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(resource=strict_patient(gender="x"), term_maps=[])
            proc = run_strict(req, audit_file, args=["--audit-errors"])
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            err = json.loads(proc.stderr)["error"]
            self.assertEqual(err["type"], "FhirValidationError")
            self.assertIn("Patient.gender", err["message"])
            (record,) = read_audit_lines(audit_file)
            self.assertEqual(record["decision"], "rejected")
            self.assertEqual(record["phase"], "validation")
            self.assertEqual(record["status"], 400)
            self.assertEqual(record["error_type"], "FhirValidationError")
            self.assertEqual(record["request_id"], req["audit_context"]["request_id"])

    def test_audit_chain_with_strict_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            ok = run_strict(
                base_request(resource=strict_patient()),
                audit_file,
                args=["--audit-chain"],
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)
            bad = run_strict(
                base_request(resource=strict_patient(active=1), term_maps=[]),
                audit_file,
                args=["--audit-chain", "--audit-errors"],
            )
            self.assertEqual(bad.returncode, 2, bad.stderr)
            proc = run_verify(audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["line_count"], 2)

    def test_verify_audit_with_strict_types_is_input_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            result = subprocess.run(
                [
                    sys.executable,
                    GATEWAY,
                    "--strict-types",
                    "--verify-audit",
                    "--audit-file",
                    audit_file,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            err = json.loads(result.stderr)["error"]
            self.assertEqual(err["type"], "InputError")
            self.assertFalse(os.path.exists(audit_file))


REQUIRE_ARGS = ["--require-mappings"]


def run_require(payload, audit_file, args=None, raw=None):
    return run(payload, audit_file, args=REQUIRE_ARGS + (args or []), raw=raw)


LOINC_MAP = {
    "system": "http://loinc.org",
    "code": "8867-4",
    "target_system": "http://snomed.info/sct",
    "target_code": "8499000",
}


def mapped_request(**overrides):
    """base_request 的 Observation 编码与 term_maps 精确命中。"""
    return base_request(term_maps=[dict(LOINC_MAP)], **overrides)


def observation_with_codings(codings, **overrides):
    resource = {
        "resourceType": "Observation",
        "id": "obs-1",
        "status": "final",
        "code": {"coding": codings},
    }
    resource.update(overrides)
    return resource


def coding(system, code):
    return {"system": system, "code": code}


class RequireMappingsSuccessTests(unittest.TestCase):
    def test_all_codings_mapped_output_shape_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run_require(mapped_request(), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(
                list(out.keys()),
                ["status", "resource_type", "resource", "mappings", "audit"],
            )
            self.assertEqual(out["status"], "accepted")
            self.assertEqual(
                out["mappings"],
                [
                    {
                        "source": {"system": "http://loinc.org", "code": "8867-4"},
                        "status": "mapped",
                        "target": {
                            "system": "http://snomed.info/sct",
                            "code": "8499000",
                        },
                    }
                ],
            )
            (record,) = read_audit_lines(audit_file)
            self.assertEqual(record["decision"], "accepted")

    def test_resource_without_codings_accepted_with_empty_maps(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(
                resource={"resourceType": "Patient", "id": "pat-1"},
                term_maps=[],
            )
            proc = run_require(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["mappings"], [])

    def test_multiple_and_nested_codings_all_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = observation_with_codings(
                [coding("http://loinc.org", "8867-4"), coding("http://a", "b")],
                valueCodeableConcept={"coding": [coding("http://c", "d")]},
            )
            req = base_request(
                resource=resource,
                term_maps=[
                    {"system": "http://loinc.org", "code": "8867-4",
                     "target_system": "t1", "target_code": "T1"},
                    {"system": "http://a", "code": "b",
                     "target_system": "t2", "target_code": "T2"},
                    {"system": "http://c", "code": "d",
                     "target_system": "t3", "target_code": "T3"},
                ],
            )
            proc = run_require(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                [(m["status"], m["source"]["code"])
                 for m in json.loads(proc.stdout)["mappings"]],
                [("mapped", "8867-4"), ("mapped", "b"), ("mapped", "d")],
            )

    def test_duplicate_codings_each_required_and_listed_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = observation_with_codings(
                [coding("http://loinc.org", "8867-4"), coding("http://loinc.org", "8867-4")]
            )
            req = base_request(resource=resource, term_maps=[dict(LOINC_MAP)])
            proc = run_require(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            mappings = json.loads(proc.stdout)["mappings"]
            self.assertEqual([m["status"] for m in mappings], ["mapped", "mapped"])

    def test_non_executable_bundle_all_mapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                patient_entry("pat-1"),
                observation_entry("obs-1"),
            ]
            req = bundle_request(entries, term_maps=[dict(LOINC_MAP)])
            proc = run_require(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["resource_type"], "Bundle")
            self.assertEqual([m["status"] for m in out["mappings"]], ["mapped"])

    def test_flag_off_unmapped_still_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(term_maps=[])
            proc = run(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], "accepted")
            self.assertEqual(out["mappings"][0]["status"], "unmapped")
            self.assertIsNone(out["mappings"][0]["target"])
            self.assertEqual(len(read_audit_lines(audit_file)), 1)

    def test_success_with_audit_chain_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run_require(mapped_request(), audit_file, args=["--audit-chain"])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("audit_digest", json.loads(proc.stdout)["audit"])
            verify = run_verify(audit_file)
            self.assertEqual(verify.returncode, 0, verify.stderr)
            self.assertEqual(json.loads(verify.stdout)["line_count"], 1)


class RequireMappingsFailureTests(unittest.TestCase):
    def assert_mapping_error(self, proc, audit_file=None, file_exists=None):
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        err = json.loads(proc.stderr)["error"]
        self.assertEqual(err["type"], "TermMappingError")
        self.assertTrue(err["message"])
        if file_exists is not None:
            self.assertEqual(os.path.exists(audit_file), file_exists)

    def test_single_unmapped_coding_is_term_mapping_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run_require(base_request(term_maps=[]), audit_file)
            self.assert_mapping_error(proc, audit_file, False)

    def test_miss_message_identifies_source_system_and_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(
                resource=observation_with_codings([coding("http://x", "y")]),
                term_maps=[],
            )
            proc = run_require(req, audit_file)
            message = json.loads(proc.stderr)["error"]["message"]
            self.assertIn("http://x", message)
            self.assertIn("y", message)

    def test_first_unmapped_in_coding_order_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = observation_with_codings(
                [coding("http://loinc.org", "8867-4"), coding("http://first-miss", "1")],
                valueCodeableConcept={"coding": [coding("http://second-miss", "2")]},
            )
            req = base_request(resource=resource, term_maps=[dict(LOINC_MAP)])
            proc = run_require(req, audit_file)
            message = json.loads(proc.stderr)["error"]["message"]
            self.assertIn("http://first-miss", message)
            self.assertNotIn("second-miss", message)

    def test_non_executable_bundle_one_unmapped_whole_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request(
                [patient_entry("pat-1"), observation_entry("obs-1")],
                term_maps=[],
            )
            proc = run_require(req, audit_file)
            self.assert_mapping_error(proc, audit_file, False)

    def test_non_executable_bundle_later_entry_unmapped_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = bundle_request(
                [observation_entry("obs-1"),
                 observation_entry("obs-2", system="http://other", code="z")],
                term_maps=[dict(LOINC_MAP)],
            )
            # 首个 entry 命中、较后 entry 未命中，仍整次拒绝。
            proc = run_require(req, audit_file)
            self.assert_mapping_error(proc, audit_file, False)

    def test_invalid_term_map_item_precedes_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(term_maps=[{"system": "http://loinc.org"}])
            proc = run_require(req, audit_file)
            self.assert_mapping_error(proc, audit_file, False)

    def test_same_source_conflict_precedes_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(
                term_maps=[
                    dict(LOINC_MAP),
                    {"system": "http://loinc.org", "code": "8867-4",
                     "target_system": "http://other", "target_code": "9"},
                ]
            )
            proc = run_require(req, audit_file)
            self.assert_mapping_error(proc, audit_file, False)

    def test_term_maps_not_array_is_input_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(term_maps={})
            proc = run_require(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "InputError"
            )
            self.assertFalse(os.path.exists(audit_file))

    def test_target_match_must_be_exact_on_system_and_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            # system 相同但 code 不同：不命中。
            req = base_request(
                term_maps=[
                    {"system": "http://loinc.org", "code": "other",
                     "target_system": "t", "target_code": "T"}
                ]
            )
            proc = run_require(req, audit_file)
            self.assert_mapping_error(proc, audit_file, False)


class RequireMappingsPrecedenceTests(unittest.TestCase):
    def test_input_error_precedes_mapping_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(term_maps=[])
            req["audit_context"]["actor"] = ""
            proc = run_require(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "InputError"
            )
            self.assertFalse(os.path.exists(audit_file))

    def test_resource_validation_precedes_mapping_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(
                resource=observation_entry(status="draft"),
                term_maps=[],
            )
            proc = run_require(req, audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "FhirValidationError"
            )
            self.assertFalse(os.path.exists(audit_file))

    def test_reference_check_precedes_mapping_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = observation_with_codings(
                [coding("http://loinc.org", "8867-4")],
                subject={"reference": "#missing-contained"},
            )
            req = base_request(resource=resource, term_maps=[])
            proc = run_require(req, audit_file, args=["--check-references"])
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "FhirValidationError"
            )
            self.assertFalse(os.path.exists(audit_file))

    def test_strict_type_check_precedes_mapping_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = observation_with_codings(
                [coding("http://loinc.org", "8867-4")],
                valueString=123,
            )
            req = base_request(resource=resource, term_maps=[])
            proc = run_require(req, audit_file, args=["--strict-types"])
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "FhirValidationError"
            )
            self.assertFalse(os.path.exists(audit_file))


class RequireMappingsExecutableBundleTests(unittest.TestCase):
    def test_transaction_unmapped_entry_whole_batch_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(patient_resource("pat-1")),
                tx_entry(observation_resource("obs-1")),
            ]
            proc = run_require(executable_request(entries), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 400)
            response_entries = out["resource"]["entry"]
            self.assertEqual(
                [(e["resourceType"], e["status"]) for e in response_entries],
                [("Patient", 200), ("Observation", 400)],
            )
            issue = response_entries[1]["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "processing")
            self.assertEqual(issue["entry"], 1)
            audit = out["audit"]
            self.assertEqual(audit["status"], 400)
            self.assertEqual(audit["entry_failed"], 1)
            self.assertEqual(audit["entries"][1]["phase"], "mapping")
            self.assertEqual(audit["entries"][1]["issue_code"], "processing")

    def test_transaction_all_mapped_is_200(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(patient_resource("pat-1")),
                tx_entry(observation_resource("obs-1")),
            ]
            req = executable_request(entries, term_maps=[dict(LOINC_MAP)])
            proc = run_require(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 200)
            self.assertEqual(
                [e["status"] for e in out["resource"]["entry"]], [200, 200]
            )
            self.assertEqual(out["audit"]["entry_failed"], 0)

    def test_batch_unmapped_entry_400_others_continue_overall_200(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(observation_resource("obs-1")),
                tx_entry(patient_resource("pat-1")),
                tx_entry(observation_resource("obs-2")),
            ]
            req = executable_request(entries, bundle_type="batch")
            proc = run_require(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 200)
            self.assertEqual(out["resource"]["type"], "batch-response")
            self.assertEqual(
                [(e["resourceType"], e["status"],
                  e.get("outcome", {}).get("issue", [{}])[0].get("code"))
                 for e in out["resource"]["entry"]],
                [("Observation", 400, "processing"),
                 ("Patient", 200, None),
                 ("Observation", 400, "processing")],
            )
            audit = out["audit"]
            self.assertEqual(audit["status"], 200)
            self.assertEqual(audit["entry_total"], 3)
            self.assertEqual(audit["entry_succeeded"], 1)
            self.assertEqual(audit["entry_failed"], 2)

    def test_batch_duplicate_entries_keep_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(observation_resource("obs-1")),
                tx_entry(observation_resource("obs-1")),
            ]
            req = executable_request(entries, bundle_type="batch", term_maps=[])
            proc = run_require(req, audit_file)
            out = json.loads(proc.stdout)
            self.assertEqual(
                [e["status"] for e in out["resource"]["entry"]], [400, 400]
            )
            self.assertEqual(
                [e["index"] for e in out["audit"]["entries"]], [0, 1]
            )

    def test_single_batch_summary_without_code_or_resource_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(patient_resource("pat-secret")),
                tx_entry(observation_resource("obs-secret")),
            ]
            proc = run_require(executable_request(entries), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(audit_file, encoding="utf-8") as fh:
                raw = fh.read()
            lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            record = lines[0]
            self.assertEqual(
                set(record)
                - {"request_id", "actor", "recorded_at", "decision", "audit_id",
                   "bundle_type", "entry_total", "entry_succeeded",
                   "entry_failed", "status", "entries"},
                set(),
            )
            for audit_entry in record["entries"]:
                self.assertEqual(
                    set(audit_entry),
                    {"index", "resource_type", "phase", "status", "issue_code"},
                )
            # 不记录资源 id、编码值或映射配置。
            self.assertNotIn("pat-secret", raw)
            self.assertNotIn("obs-secret", raw)
            self.assertNotIn("8867-4", raw)
            self.assertNotIn("loinc", raw)

    def test_entry_without_codings_succeeds_under_require(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(patient_resource("pat-1")),
                tx_entry(
                    {
                        "resourceType": "Condition",
                        "id": "c1",
                        "clinicalStatus": {
                            "coding": [
                                {
                                    "system": "http://cc",
                                    "code": "active",
                                }
                            ]
                        },
                    }
                ),
            ]
            req = executable_request(
                entries,
                term_maps=[
                    {"system": "http://cc", "code": "active",
                     "target_system": "t", "target_code": "T"}
                ],
            )
            proc = run_require(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                [e["status"] for e in json.loads(proc.stdout)["resource"]["entry"]],
                [200, 200],
            )

    def test_term_index_conflict_is_processing_per_entry_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = executable_request([tx_entry(observation_resource())])
            req["term_maps"] = [
                {"system": "http://loinc.org", "code": "8867-4",
                 "target_system": "http://snomed.info/sct", "target_code": "1"},
                {"system": "http://loinc.org", "code": "8867-4",
                 "target_system": "http://snomed.info/sct", "target_code": "2"},
            ]
            proc = run_require(req, audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 400)
            entry = out["resource"]["entry"][0]
            self.assertEqual(entry["outcome"]["issue"][0]["code"], "processing")
            self.assertEqual(out["audit"]["entries"][0]["phase"], "mapping")

    def test_flag_off_executable_semantics_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            entries = [
                tx_entry(patient_resource("pat-1")),
                tx_entry(observation_resource("obs-1")),
            ]
            proc = run(executable_request(entries), audit_file)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            # 基线：term_maps 为空也逐项 200，整体 200。
            self.assertEqual(out["status"], 200)
            self.assertEqual(
                [e["status"] for e in out["resource"]["entry"]], [200, 200]
            )

    def test_strict_failure_precedes_mapping_miss_in_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            resource = observation_with_codings(
                [coding("http://loinc.org", "8867-4")],
                valueString=123,
            )
            req = executable_request(
                [tx_entry(resource)], term_maps=[]
            )
            proc = run_require(req, audit_file, args=["--strict-types"])
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], 400)
            issue = out["resource"]["entry"][0]["outcome"]["issue"][0]
            self.assertEqual(issue["code"], "processing")
            self.assertEqual(
                out["audit"]["entries"][0]["phase"], "validation"
            )


class RequireMappingsComboTests(unittest.TestCase):
    def test_audit_errors_appends_rejected_mapping_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = base_request(term_maps=[])
            proc = run_require(req, audit_file, args=["--audit-errors"])
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            err = json.loads(proc.stderr)["error"]
            self.assertEqual(err["type"], "TermMappingError")
            (record,) = read_audit_lines(audit_file)
            self.assertEqual(
                list(record.keys()),
                ["request_id", "actor", "recorded_at", "decision", "audit_id",
                 "error_type", "phase", "status"],
            )
            self.assertEqual(record["decision"], "rejected")
            self.assertEqual(record["error_type"], "TermMappingError")
            self.assertEqual(record["phase"], "mapping")
            self.assertEqual(record["status"], 400)
            self.assertTrue(record["audit_id"])
            self.assertEqual(
                record["request_id"], req["audit_context"]["request_id"]
            )
            # 拒绝记录不含编码值与映射配置。
            with open(audit_file, encoding="utf-8") as fh:
                raw = fh.read()
            self.assertNotIn("8867-4", raw)
            self.assertNotIn("loinc", raw)

    def test_without_audit_errors_miss_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            proc = run_require(base_request(term_maps=[]), audit_file)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(os.path.exists(audit_file))

    def test_chained_rejection_record_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            ok = run_require(mapped_request(), audit_file, args=["--audit-chain"])
            self.assertEqual(ok.returncode, 0, ok.stderr)
            bad = run_require(
                base_request(term_maps=[]),
                audit_file,
                args=["--audit-chain", "--audit-errors"],
            )
            self.assertEqual(bad.returncode, 2, bad.stderr)
            verify = run_verify(audit_file)
            self.assertEqual(verify.returncode, 0, verify.stderr)
            self.assertEqual(json.loads(verify.stdout)["line_count"], 2)

    def test_broken_chain_rejection_is_write_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            with open(audit_file, "w", encoding="utf-8") as fh:
                fh.write('{"not": "a chain line"}\n')
            proc = run_require(
                base_request(term_maps=[]),
                audit_file,
                args=["--audit-chain", "--audit-errors"],
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "AuditWriteError"
            )
            with open(audit_file, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), '{"not": "a chain line"}\n')

    def test_unwritable_audit_target_is_write_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "missing-dir", "audit.jsonl")
            proc = run_require(mapped_request(), nested)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            self.assertEqual(
                json.loads(proc.stderr)["error"]["type"], "AuditWriteError"
            )

    def test_missing_audit_file_arg_is_write_error(self):
        proc = run(mapped_request(), args=REQUIRE_ARGS)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            json.loads(proc.stderr)["error"]["type"], "AuditWriteError"
        )

    def test_verify_audit_with_require_mappings_is_input_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            result = subprocess.run(
                [
                    sys.executable,
                    GATEWAY,
                    "--require-mappings",
                    "--verify-audit",
                    "--audit-file",
                    audit_file,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertEqual(
                json.loads(result.stderr)["error"]["type"], "InputError"
            )
            self.assertFalse(os.path.exists(audit_file))

    def test_audit_find_with_require_mappings_is_input_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            result = subprocess.run(
                [
                    sys.executable,
                    GATEWAY,
                    "--require-mappings",
                    "--audit-find",
                    "--audit-file",
                    audit_file,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertEqual(
                json.loads(result.stderr)["error"]["type"], "InputError"
            )
            self.assertFalse(os.path.exists(audit_file))

    def test_with_check_references_and_strict_types_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = os.path.join(tmp, "audit.jsonl")
            req = mapped_request()
            req["resource"]["name"] = [{"family": "Doe"}]
            proc = run_require(
                req, audit_file, args=["--check-references", "--strict-types"]
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
