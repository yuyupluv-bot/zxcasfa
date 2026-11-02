# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PortainerFirebaseV153(unittest.TestCase):
    def test_base64_credentials_supported(self):
        source = (ROOT / "bot/push_service.py").read_text("utf-8")
        self.assertIn('FIREBASE_SERVICE_ACCOUNT_B64', source)
        self.assertIn('base64.b64decode', source)
        self.assertIn('credentials.Certificate(json.loads(decoded))', source)

    def test_compose_passes_portainer_variable(self):
        compose = (ROOT / "docker-compose.yml").read_text("utf-8")
        self.assertIn(
            'FIREBASE_SERVICE_ACCOUNT_B64: ${FIREBASE_SERVICE_ACCOUNT_B64:-}',
            compose,
        )

    def test_push_diagnostics_are_logged(self):
        source = (ROOT / "bot/push_service.py").read_text("utf-8")
        self.assertIn('Push result driver=%s event=%s devices=%s sent=%s', source)
        self.assertIn('Firebase credentials are missing', source)


if __name__ == "__main__":
    unittest.main()
