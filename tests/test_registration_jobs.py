"""Unit tests for job state machine and error classification."""

from __future__ import annotations

import unittest

from registration_jobs import (
    ErrorClass,
    JobState,
    RegistrationJob,
    can_transition,
    classify_error,
    email_hash,
    experiment_bucket,
    in_experiment,
    is_retryable,
    new_job_id,
    redact_text,
)


class JobStateMachineTests(unittest.TestCase):
    def test_legal_path(self) -> None:
        job = RegistrationJob(job_id=new_job_id(), session_id="s1", route_id="route-1")
        job.transition(JobState.SIGNUP_RUNNING)
        job.transition(JobState.SSO_OBTAINED)
        job.transition(JobState.MINT_QUEUED)
        job.transition(JobState.MINT_RUNNING)
        job.transition(JobState.TOKEN_RECEIVED)
        job.transition(JobState.PROBE_RUNNING)
        job.transition(JobState.PROBE_PASSED)
        job.transition(JobState.AUTH_IMPORTED)
        self.assertEqual(job.state, JobState.AUTH_IMPORTED.value)

    def test_illegal_transition(self) -> None:
        job = RegistrationJob(job_id="j", session_id="s", route_id="route-1")
        with self.assertRaises(ValueError):
            job.transition(JobState.AUTH_IMPORTED)

    def test_can_transition_helper(self) -> None:
        self.assertTrue(can_transition(JobState.MINT_RUNNING, JobState.BROWSER_DENIED))
        self.assertFalse(can_transition(JobState.AUTH_IMPORTED, JobState.MINT_QUEUED))

    def test_public_dict_hides_secrets(self) -> None:
        job = RegistrationJob(
            job_id="j1",
            session_id="s1",
            route_id="route-1",
            sso_ref="/app/data/pending_sso/s1.json",
        )
        pub = job.to_public_dict()
        self.assertTrue(pub["has_sso_ref"])
        self.assertNotIn("sso", pub)


class ErrorClassTests(unittest.TestCase):
    def test_classify(self) -> None:
        self.assertEqual(classify_error("rate_limited by upstream"), ErrorClass.RATE_LIMITED)
        self.assertEqual(classify_error("browser timeout"), ErrorClass.BROWSER_TIMEOUT)
        self.assertEqual(classify_error("access_denied"), ErrorClass.BROWSER_DENIED)
        self.assertEqual(classify_error("sso 无效"), ErrorClass.SSO_INVALID)
        self.assertTrue(is_retryable(ErrorClass.TRANSIENT_NETWORK))
        self.assertFalse(is_retryable(ErrorClass.SSO_INVALID))


class UtilityTests(unittest.TestCase):
    def test_email_hash_stable(self) -> None:
        self.assertEqual(email_hash("A@B.com"), email_hash("a@b.com"))
        self.assertEqual(len(email_hash("a@b.com")), 16)

    def test_redact(self) -> None:
        text = "user a@b.com token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaa.bbb secret"
        out = redact_text(text, secrets=["secret"])
        self.assertNotIn("a@b.com", out)
        self.assertNotIn("eyJ", out)
        self.assertNotIn("secret", out)

    def test_experiment_bucket_stable(self) -> None:
        b1 = experiment_bucket("cookie_bundle", "sess-1")
        b2 = experiment_bucket("cookie_bundle", "sess-1")
        self.assertEqual(b1, b2)
        self.assertTrue(0 <= b1 < 10000)
        self.assertFalse(in_experiment("sess-1", 0))
        self.assertTrue(in_experiment("sess-1", 100))

    def test_experiment_percent_distribution(self) -> None:
        """10% must not collapse to ~100% (old digest[0] bug)."""
        n = 2000
        hits = sum(
            1 for i in range(n) if in_experiment(f"sess-{i}", 10, experiment_id="cookie_bundle")
        )
        rate = hits / n
        # Expect ~0.10; allow wide but not catastrophic band
        self.assertGreater(rate, 0.05)
        self.assertLess(rate, 0.18)


if __name__ == "__main__":
    unittest.main()
