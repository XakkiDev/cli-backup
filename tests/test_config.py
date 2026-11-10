import unittest
from cli_backup.config import resolve, validate_project


class ConfigurationTests(unittest.TestCase):
    def test_precedence(self):
        env = {"CLI_BACKUP_PROJECT": "global"}
        project = {"CLI_BACKUP_PROJECT": "project"}
        self.assertEqual(resolve("project", "argument", project, env), "argument")
        self.assertEqual(resolve("project", None, project, env), "project")
        self.assertEqual(resolve("project", None, {}, env), "global")

    def test_reject_paths(self):
        for value in ("../other", "", ".", "a/b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_project(value)
