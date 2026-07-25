import argparse
import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import leadme_deploy as ld


def make_sqlite(path, rows=(("one",),)):
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE records (value TEXT)")
        conn.executemany("INSERT INTO records VALUES (?)", rows)
        conn.commit()


def sqlite_values(path):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return [row[0] for row in conn.execute("SELECT value FROM records ORDER BY value")]


class ProductionDataBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        self.backups = self.root / "backups"
        self.app.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def add_required_db(self, rows=(("one",),)):
        make_sqlite(self.app / "data/app_auth.db", rows)

    def snapshot(self, timestamp="20260725-120000", **kwargs):
        return ld.create_backup_snapshot(
            self.app,
            self.backups,
            "a" * 40,
            timestamp=timestamp,
            **kwargs,
        )

    def test_sqlite_online_backup_is_valid_and_consistent(self):
        self.add_required_db((("alpha",), ("beta",)))
        result = self.snapshot()
        copied = Path(result["backup_dir"]) / "data/app_auth.db"
        self.assertEqual(sqlite_values(copied), ["alpha", "beta"])
        self.assertTrue(ld._sqlite_integrity(copied))
        manifest = json.loads(Path(result["manifest"]).read_text())
        item = next(item for item in manifest["items"] if item["path"] == "data/app_auth.db")
        self.assertEqual(item["sqlite_integrity"], "ok")
        self.assertGreater(item["size"], 0)

    def test_integrity_failure_halts(self):
        self.add_required_db()
        with mock.patch.object(ld, "_sqlite_integrity", return_value=False):
            with self.assertRaises(ld.BackupError):
                self.snapshot()

    def test_required_database_missing_halts(self):
        with self.assertRaisesRegex(ld.BackupError, "required backup source"):
            self.snapshot()

    def test_optional_absence_is_recorded_without_halting(self):
        self.add_required_db()
        result = self.snapshot()
        manifest = json.loads(Path(result["manifest"]).read_text())
        absent = {item["path"] for item in manifest["not_present"]}
        self.assertIn("reports/history.json", absent)
        self.assertIn("exports", absent)
        self.assertTrue(manifest["verified"])

    def test_files_and_directories_preserve_relative_structure(self):
        self.add_required_db()
        history = self.app / "reports/history.json"
        history.parent.mkdir(parents=True)
        history.write_text('[{"report":"saved/a.html"}]', encoding="utf-8")
        saved = self.app / "reports/saved/nested"
        saved.mkdir(parents=True)
        (saved / "report.html").write_text("<h1>Saved</h1>", encoding="utf-8")
        exports = self.app / "exports/metadata"
        exports.mkdir(parents=True)
        (self.app / "exports/leads.csv").write_text("domain\nexample.com\n", encoding="utf-8")
        (exports / "leads.json").write_text('{"partial":true}', encoding="utf-8")
        owners = self.app / "data/leadbot_export_owners.json"
        owners.write_text('{"leads.csv":{"email":"u@example.com"}}', encoding="utf-8")

        result = self.snapshot()
        destination = Path(result["backup_dir"])
        self.assertEqual(
            (destination / "reports/history.json").read_text(),
            history.read_text(),
        )
        self.assertEqual(
            (destination / "reports/saved/nested/report.html").read_text(),
            "<h1>Saved</h1>",
        )
        self.assertEqual(
            (destination / "exports/metadata/leads.json").read_text(),
            '{"partial":true}',
        )
        self.assertEqual(
            (destination / "data/leadbot_export_owners.json").read_text(),
            owners.read_text(),
        )

    def test_symlink_in_protected_tree_halts_without_following(self):
        self.add_required_db()
        outside = self.root / "outside.txt"
        outside.write_text("must not copy", encoding="utf-8")
        exports = self.app / "exports"
        exports.mkdir()
        (exports / "escape.csv").symlink_to(outside)
        with self.assertRaisesRegex(ld.BackupError, "symlink"):
            self.snapshot()
        self.assertFalse((self.backups / "20260725-120000/exports/escape.csv").exists())

    def test_manifest_has_expected_entries_and_no_source_secrets(self):
        self.add_required_db((("password=super-secret-token",),))
        result = self.snapshot()
        raw = Path(result["manifest"]).read_text()
        manifest = json.loads(raw)
        self.assertEqual(manifest["production_commit"], "a" * 40)
        self.assertEqual(manifest["items"][0]["path"], "data/app_auth.db")
        self.assertNotIn("super-secret", raw)
        self.assertNotIn("password=", raw)

    def _verified_old_backup(self, timestamp):
        directory = self.backups / timestamp
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "backup-manifest.json").write_text(
            json.dumps({"verified": True}), encoding="utf-8"
        )
        return directory

    def test_retention_keeps_last_ten_verified_and_legacy_files(self):
        self.add_required_db()
        for index in range(10):
            self._verified_old_backup(f"20260724-{index:06d}")
        legacy = self.backups / "app_auth-legacy.db"
        legacy.write_text("legacy", encoding="utf-8")

        self.snapshot(timestamp="20260725-120000")
        verified = [
            path
            for path in self.backups.iterdir()
            if path.is_dir()
            and (path / "backup-manifest.json").exists()
            and json.loads((path / "backup-manifest.json").read_text()).get("verified")
        ]
        self.assertEqual(len(verified), 10)
        self.assertTrue((self.backups / "20260725-120000").exists())
        self.assertTrue(legacy.exists())

    def test_failed_new_backup_does_not_run_retention(self):
        for index in range(11):
            self._verified_old_backup(f"20260724-{index:06d}")
        with self.assertRaises(ld.BackupError):
            self.snapshot(timestamp="20260725-120000")
        old_verified = [
            path for path in self.backups.iterdir()
            if path.name.startswith("20260724-")
        ]
        self.assertEqual(len(old_verified), 11)

    def test_backup_destination_cannot_be_inside_application(self):
        self.add_required_db()
        with self.assertRaisesRegex(ld.BackupError, "outside"):
            ld.create_backup_snapshot(
                self.app,
                self.app / "exports/backups",
                "a" * 40,
                timestamp="20260725-120000",
            )

    def test_remote_backup_payload_runs_the_checked_in_backup_code(self):
        self.add_required_db()

        def execute_stdin(_command, timeout=None, input_text=None):
            completed = subprocess.run(
                [sys.executable, "-"],
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            return ld.CmdResult(completed.returncode, completed.stdout, completed.stderr)

        with mock.patch.object(ld, "PROD_APP_PATH", str(self.app)), mock.patch.object(
            ld, "PROD_BACKUPS_DIR", str(self.backups)
        ), mock.patch.object(ld, "run_remote", side_effect=execute_stdin):
            result = ld.run_remote_backup("a" * 40, "20260725-120000")

        self.assertTrue(result.ok, result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["ok"])
        self.assertTrue(Path(payload["manifest"]).is_file())


class BackupToolOutputTests(unittest.TestCase):
    def test_dry_run_describes_expanded_verified_backup(self):
        local = {
            "branch": "main",
            "local_sha": "a" * 40,
            "divergence": "in-sync",
            "ahead": 0,
            "behind": 0,
            "fetch_ok": True,
            "fetch_error": "",
            "origin_sha": "a" * 40,
        }
        with mock.patch.object(ld, "gather_local_status", return_value=local), mock.patch.object(
            ld, "local_python_for_compile", return_value="python"
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                rc = ld.cmd_dry_run(argparse.Namespace())
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("online SQLite backup + integrity_check", text)
        self.assertIn("reports/saved", text)
        self.assertIn("exports", text)
        self.assertIn("backup-manifest.json", text)
        self.assertIn("latest 10 verified", text)
        self.assertNotIn("cp /var/www/leadmeleads/data/app_auth.db", text)

    def test_rollback_info_uses_backup_directory_and_manifest(self):
        state = {
            "previous_production_commit": "a" * 40,
            "deployed_commit": "b" * 40,
            "backup_path": "/var/www/leadmeleads-backups/20260725-120000",
            "last_deploy_timestamp": "2026-07-25T12:00:00+00:00",
        }
        with mock.patch.object(ld, "read_state", return_value=state), mock.patch.object(
            ld, "remote_hostname", return_value=("prod", mock.Mock())
        ), mock.patch.object(ld, "remote_head_sha", return_value="b" * 40), mock.patch.object(
            ld,
            "remote_backups_listing",
            return_value=["/var/www/leadmeleads-backups/20260725-120000"],
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                rc = ld.cmd_rollback_info(argparse.Namespace())
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("backup-manifest.json", text)
        self.assertIn("/data/app_auth.db", text)
        self.assertIn("latest verified backup dirs", text)


if __name__ == "__main__":
    unittest.main()
