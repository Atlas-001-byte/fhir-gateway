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


def run_chain(payload, audit_file, raw=None):
    return run(payload, audit_file, args=CHAIN_ARGS, raw=raw)


def run_verify(audit_file, stdin=subprocess.DEVNULL):
    """校验模式：默认不接管道（DEVNULL），顺带验证其不读取标准输入。"""
    cmd = [sys.executable, GATEWAY, "--verify-audit", "--audit-file", audit_file]
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
