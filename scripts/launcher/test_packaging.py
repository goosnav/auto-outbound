"""Release-boundary checks that do not require a native desktop."""

import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


package = load_module("package_universal")


class ReleaseBoundaryTests(unittest.TestCase):
    def test_secret_paths_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / ".env"
            secret.write_text("provider=private")
            with zipfile.ZipFile(root / "test.zip", "w") as archive:
                with self.assertRaises(SystemExit):
                    package.add_path(archive, secret, Path("app/.env"))

    def test_executable_mode_survives_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "launcher"
            launcher.write_text("executable")
            launcher.chmod(0o755)
            with zipfile.ZipFile(root / "test.zip", "w") as archive:
                package.add_path(archive, launcher, Path("Product/launcher"))
            with zipfile.ZipFile(root / "test.zip") as archive:
                self.assertTrue(archive.getinfo("Product/launcher").external_attr >> 16 & 0o100)

    def test_source_checkout_cannot_masquerade_as_release(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                package.validate_app(Path(directory))

    def test_placeholder_customer_documents_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "LICENSE.txt"
            document.write_text("Replace this placeholder with the real license.")
            with self.assertRaises(SystemExit):
                package.validate_customer_document(document)


if __name__ == "__main__":
    unittest.main()
