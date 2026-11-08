import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_private  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class CheckPrivateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "dev")
        git(self.repo, "config", "user.email", "dev@users.noreply.github.com")
        self.list = Path(tmp.name) / "private.txt"
        self.list.write_text("# mine\nre:\\bAlex\\b\nmyhost\n10.1.2.\n", encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)
        env = patch.dict(os.environ, {"OPENCLAVE_PRIVATE_STRINGS": str(self.list)})
        env.start()
        self.addCleanup(env.stop)

    def check(self, *args) -> tuple[int, str]:
        err = io.StringIO()
        with redirect_stderr(err):
            code = check_private.main(list(args))
        return code, err.getvalue()

    def stage(self, name, text):
        (self.repo / name).write_text(text, encoding="utf-8")
        git(self.repo, "add", name)

    def test_clean_change_passes(self):
        self.stage("a.py", "print('hello')\n# alexander is not a match\n")
        self.assertEqual(self.check("--staged"), (0, ""))

    def test_added_line_with_personal_detail_is_refused(self):
        self.stage("a.py", "ok\nHOST = 'MyHost.local'\nIP = '10.1.2.7'\n")
        code, err = self.check("--staged")
        self.assertEqual(code, 1)
        self.assertIn("a.py:2: myhost", err)
        self.assertIn("a.py:3: 10.1.2.", err)

    def test_only_added_lines_count(self):
        self.stage("a.py", "Alex wrote this\n")
        git(self.repo, "commit", "-q", "--no-verify", "-m", "old")
        self.stage("a.py", "Alex wrote this\nnew line\n")
        self.assertEqual(self.check("--staged")[0], 0)
        self.assertEqual(self.check("--all")[0], 1, "a full sweep still sees it")

    def test_regex_patterns_are_case_sensitive_words(self):
        self.stage("a.py", "alex = 1  # lower case variable\n")
        self.assertEqual(self.check("--staged")[0], 0)

    def test_file_name_and_author_are_checked(self):
        self.stage("myhost-notes.md", "x\n")
        self.assertIn("myhost-notes.md (file name)", self.check("--staged")[1])
        git(self.repo, "rm", "-q", "--cached", "myhost-notes.md")
        git(self.repo, "config", "user.name", "Alex")
        self.stage("b.py", "x\n")
        code, err = self.check("--staged")
        self.assertEqual(code, 1)
        self.assertIn("commit author", err)

    def test_missing_list_passes_with_a_note(self):
        self.list.unlink()
        self.stage("a.py", "myhost\n")
        code, err = self.check("--staged")
        self.assertEqual(code, 0)
        self.assertIn("no list", err)


if __name__ == "__main__":
    unittest.main()
