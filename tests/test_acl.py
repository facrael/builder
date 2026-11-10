import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from config import parse_and_validate_config


class TestACL(unittest.TestCase):
    def test_user_project_acl_matrix(self):
        env = {
            "ADMIN_TG_ID": "100",
            "PROJECTS": "CTP, KORPUS2",
            "PROJECT_CTP_CODE": "ЦТП",
            "PROJECT_CTP_NAME": "ЦТП",
            "PROJECT_CTP_CLOUD_OK": "false",
            "PROJECT_CTP_USERS": "200, 300",
            "PROJECT_KORPUS2_CODE": "КОРПУС-2",
            "PROJECT_KORPUS2_NAME": "Корпус 2",
            "PROJECT_KORPUS2_CLOUD_OK": "true",
            "PROJECT_KORPUS2_USERS": "300, 400",
        }
        cfg = parse_and_validate_config(env)

        # User 200: only CTP
        self.assertEqual(set(cfg.get_user_projects(200)), {"ЦТП"})
        self.assertIn("ЦТП", cfg.get_user_projects(200))
        self.assertNotIn("КОРПУС-2", cfg.get_user_projects(200))

        # User 300: both CTP and KORPUS-2
        self.assertEqual(set(cfg.get_user_projects(300)), {"ЦТП", "КОРПУС-2"})

        # User 400: only KORPUS-2
        self.assertEqual(set(cfg.get_user_projects(400)), {"КОРПУС-2"})
        self.assertNotIn("ЦТП", cfg.get_user_projects(400))

        # User 500: unknown user (no projects, not allowed)
        self.assertEqual(cfg.get_user_projects(500), [])
        self.assertFalse(cfg.is_user_allowed(500))

        # Admin: admin has system access, but only sees projects they are explicitly listed in
        self.assertTrue(cfg.is_admin(100))
        self.assertEqual(cfg.get_user_projects(100), [])


if __name__ == "__main__":
    unittest.main()
