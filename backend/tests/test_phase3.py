from datetime import date, datetime, timezone
import unittest

from app.evidence import (
    ChecklistRequirement,
    ChecklistVersion,
    DriveSearchResult,
    EmailPolicy,
    EmailRequest,
    EmailTemplate,
    EvidenceCollector,
    EvidencePolicyError,
    EvidenceScope,
    GmailActionStatus,
    GmailDraft,
    GmailSearchResult,
    GmailSendResult,
    GmailRequestService,
    evaluate_checklist,
)


class DriveClient:
    def __init__(self, results):
        self.results = results

    def search_evidence(self, scope):
        return self.results


class GmailEvidenceClient:
    def __init__(self, results):
        self.results = results

    def search_evidence(self, scope):
        return self.results


def scope():
    return EvidenceScope(
        frozenset({"folder-close"}),
        "close@example.test",
        frozenset({"INBOX", "LABEL_CLOSE"}),
        date(2026, 7, 1),
        date(2026, 7, 31),
    )


class EvidenceTests(unittest.TestCase):
    def test_collection_is_scoped_and_checklist_is_deterministic(self):
        observed = datetime(2026, 7, 10, tzinfo=timezone.utc)
        batch = EvidenceCollector(
            DriveClient(
                [DriveSearchResult("doc-1", "folder-close", "statement.pdf", "application/pdf", observed, "hash-doc", frozenset({"bank-statement"}))]
            ),
            GmailEvidenceClient(
                [GmailSearchResult("msg-1", "thread-1", "close@example.test", frozenset({"LABEL_CLOSE"}), observed, "controller@example.test", "Invoice support", "hash-mail", frozenset({"invoice-support"}))]
            ),
        ).collect(scope())
        result = evaluate_checklist(
            ChecklistVersion(
                "close-v1",
                1,
                (
                    ChecklistRequirement("bank", "Bank statement", frozenset({"bank-statement"})),
                    ChecklistRequirement("invoice", "Invoice support", frozenset({"invoice-support"})),
                ),
            ),
            batch,
        )
        self.assertTrue(result.ready)
        self.assertEqual(result.missing, ())

    def test_out_of_scope_evidence_blocks_collection(self):
        observed = datetime(2026, 7, 10, tzinfo=timezone.utc)
        with self.assertRaises(EvidencePolicyError):
            EvidenceCollector(
                DriveClient([DriveSearchResult("doc-1", "wrong-folder", "x", "text/plain", observed, "hash")]),
                GmailEvidenceClient([]),
            ).collect(scope())

    def test_out_of_period_or_unlabeled_results_are_filtered_not_fatal(self):
        in_period = datetime(2026, 7, 10, tzinfo=timezone.utc)
        previous_period = datetime(2026, 6, 30, tzinfo=timezone.utc)
        batch = EvidenceCollector(
            DriveClient(
                [
                    DriveSearchResult("old-doc", "folder-close", "old.pdf", "application/pdf", previous_period, "hash-old"),
                    DriveSearchResult("current-doc", "folder-close", "current.pdf", "application/pdf", in_period, "hash-current"),
                ]
            ),
            GmailEvidenceClient(
                [
                    GmailSearchResult("unlabeled", "thread-1", "close@example.test", frozenset({"OTHER"}), in_period, "sender@example.test", "Other", "hash-other"),
                    GmailSearchResult("current-mail", "thread-2", "close@example.test", frozenset({"LABEL_CLOSE"}), in_period, "sender@example.test", "Close", "hash-mail"),
                ]
            ),
        ).collect(scope())
        self.assertEqual({item.source_id for item in batch.items}, {"current-doc", "current-mail"})


class GmailClient:
    def __init__(self, *, send_error=False, search_result=None):
        self.send_error = send_error
        self.search_result = search_result
        self.created = []

    def create_request_draft(self, recipient, subject, body, marker):
        self.created.append(marker)
        return GmailDraft("draft-1", marker)

    def send_approved_request(self, draft_id):
        if self.send_error:
            raise TimeoutError("send timed out")
        return GmailSendResult("message-1", "thread-1")

    def search_sent_by_marker(self, marker):
        return self.search_result


def request():
    return EmailRequest(
        "controller@example.test",
        EmailTemplate("missing-doc-v1", "Missing close document", "Please provide the requested close evidence."),
        ("bank-statement",),
    )


def policy():
    return EmailPolicy(
        frozenset({"controller@example.test"}),
        frozenset(),
        frozenset({"missing-doc-v1"}),
    )


class EmailPolicyTests(unittest.TestCase):
    def test_allowlisted_request_sends_once_and_is_idempotent(self):
        client = GmailClient()
        service = GmailRequestService(client, policy())
        first = service.prepare(request())
        second = service.prepare(request())
        self.assertEqual(first.action_id, second.action_id)
        sent = service.send(first.action_id)
        self.assertEqual(sent.status, GmailActionStatus.SENT)
        self.assertEqual(service.send(first.action_id).gmail_message_id, "message-1")
        self.assertEqual(len(client.created), 1)

    def test_non_allowlisted_or_prohibited_request_is_rejected(self):
        service = GmailRequestService(GmailClient(), policy())
        with self.assertRaises(EvidencePolicyError):
            service.prepare(
                EmailRequest(
                    "outside@example.com",
                    request().template,
                    ("bank-statement",),
                )
            )
        with self.assertRaises(EvidencePolicyError):
            service.prepare(
                EmailRequest(
                    "controller@example.test",
                    EmailTemplate("missing-doc-v1", "Missing", "Please send your bank account password."),
                    ("bank-statement",),
                )
            )

    def test_ambiguous_send_stops_without_resend(self):
        client = GmailClient(send_error=True, search_result=None)
        service = GmailRequestService(client, policy())
        execution = service.prepare(request())
        result = service.send(execution.action_id)
        self.assertEqual(result.status, GmailActionStatus.OUTCOME_UNKNOWN)
        self.assertEqual(len(client.created), 1)

    def test_proven_absence_is_a_failed_send_not_a_success(self):
        client = GmailClient(send_error=True, search_result=[])
        service = GmailRequestService(client, policy())
        execution = service.prepare(request())
        result = service.send(execution.action_id)
        self.assertEqual(result.status, GmailActionStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
