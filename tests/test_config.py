import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from config import parse_and_validate_config


class TestConfig(unittest.TestCase):
    def test_valid_config(self):
        env = {
            "ADMIN_TG_ID": "111111111",
            "PROJECTS": "OBJ1, OBJ2",
            "PROJECT_OBJ1_CODE": "ЦТП",
            "PROJECT_OBJ1_NAME": "Центральный тепловой пункт",
            "PROJECT_OBJ1_CLOUD_OK": "false",
            "PROJECT_OBJ1_USERS": "111111111, 222222222",
            "PROJECT_OBJ2_CODE": "КОРПУС-2",
            "PROJECT_OBJ2_NAME": "Корпус 2",
            "PROJECT_OBJ2_CLOUD_OK": "true",
            "PROJECT_OBJ2_USERS": "111111111, 333333333",
        }
        cfg = parse_and_validate_config(env)
        self.assertEqual(cfg.admin_tg_id, 111111111)
        self.assertIn("ЦТП", cfg.projects)
        self.assertIn("КОРПУС-2", cfg.projects)
        self.assertFalse(cfg.ok_for_cloud("ЦТП"))
        self.assertTrue(cfg.ok_for_cloud("КОРПУС-2"))
        self.assertTrue(cfg.is_user_allowed(222222222))
        self.assertFalse(cfg.is_user_allowed(999999999))
        self.assertEqual(set(cfg.get_user_projects(222222222)), {"ЦТП"})
        self.assertEqual(set(cfg.get_user_projects(111111111)), {"ЦТП", "КОРПУС-2"})

    def test_missing_admin_fails(self):
        env = {
            "PROJECTS": "OBJ1",
            "PROJECT_OBJ1_CODE": "ЦТП",
            "PROJECT_OBJ1_USERS": "123",
        }
        with self.assertRaises(ValueError):
            parse_and_validate_config(env)

    def test_invalid_admin_fails(self):
        env = {
            "ADMIN_TG_ID": "-5",
            "PROJECTS": "OBJ1",
            "PROJECT_OBJ1_CODE": "ЦТП",
            "PROJECT_OBJ1_USERS": "123",
        }
        with self.assertRaises(ValueError):
            parse_and_validate_config(env)

    def test_empty_projects_fails(self):
        env = {
            "ADMIN_TG_ID": "111111111",
            "PROJECTS": "",
        }
        with self.assertRaises(ValueError):
            parse_and_validate_config(env)

    def test_missing_project_code_fails(self):
        env = {
            "ADMIN_TG_ID": "111111111",
            "PROJECTS": "OBJ1",
            "PROJECT_OBJ1_USERS": "123",
        }
        with self.assertRaises(ValueError):
            parse_and_validate_config(env)

    def test_duplicate_project_code_fails(self):
        env = {
            "ADMIN_TG_ID": "111111111",
            "PROJECTS": "OBJ1, OBJ2",
            "PROJECT_OBJ1_CODE": "ЦТП",
            "PROJECT_OBJ1_USERS": "123",
            "PROJECT_OBJ2_CODE": "ЦТП",
            "PROJECT_OBJ2_USERS": "456",
        }
        with self.assertRaises(ValueError):
            parse_and_validate_config(env)

    def test_duplicate_user_id_within_project_fails(self):
        env = {
            "ADMIN_TG_ID": "111111111",
            "PROJECTS": "OBJ1",
            "PROJECT_OBJ1_CODE": "ЦТП",
            "PROJECT_OBJ1_USERS": "123, 456, 123",
        }
        with self.assertRaises(ValueError):
            parse_and_validate_config(env)

    def test_empty_user_list_means_zero_access(self):
        env = {
            "ADMIN_TG_ID": "111111111",
            "PROJECTS": "OBJ1",
            "PROJECT_OBJ1_CODE": "ЦТП",
            "PROJECT_OBJ1_USERS": "",
        }
        cfg = parse_and_validate_config(env)
        self.assertEqual(len(cfg.projects["ЦТП"].users), 0)
        self.assertFalse(cfg.is_user_allowed(999999999))
        self.assertEqual(cfg.get_user_projects(999999999), [])


if __name__ == "__main__":
    unittest.main()
