#!/usr/bin/env python3
"""Regression tests for dual-write authoritative-backend failure handling."""
import unittest
from unittest.mock import patch

from app.services.database import _raise_if_authoritative_write_failed


class AuthoritativeWriteFailureTests(unittest.TestCase):
    def test_postgres_success_tolerates_sqlite_backup_failure(self):
        with patch("app.services.database._use_postgres", return_value=True):
            _raise_if_authoritative_write_failed(
                "record_payment_success",
                sqlite_ok=False,
                pg_ok=True,
                detail="key=pay_test",
            )

    def test_postgres_configured_requires_postgres_success(self):
        with patch("app.services.database._use_postgres", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "postgres write failed"):
                _raise_if_authoritative_write_failed(
                    "record_payment_success",
                    sqlite_ok=True,
                    pg_ok=False,
                    detail="key=pay_test",
                )

    def test_sqlite_mode_requires_sqlite_success(self):
        with patch("app.services.database._use_postgres", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "sqlite write failed"):
                _raise_if_authoritative_write_failed(
                    "record_payment_success",
                    sqlite_ok=False,
                    pg_ok=True,
                    detail="key=pay_test",
                )


if __name__ == "__main__":
    unittest.main()
