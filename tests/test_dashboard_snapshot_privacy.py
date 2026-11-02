"""A shared browser must not replay one dashboard user's private data to another."""

import shutil
import subprocess
import unittest
from pathlib import Path


class DashboardSnapshotPrivacyTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is needed to exercise dashboard JavaScript")
    def test_snapshots_do_not_persist_or_restore_between_users(self):
        html = (Path(__file__).resolve().parent.parent / "cos/static/index.html").read_text()
        code = html[html.index("// Old dashboard snapshots"):html.index("async function loadOverview()")]
        setup = '''
const data = {"snap:overview": JSON.stringify({private: "other user's runs"})};
const localStorage = {getItem: k => data[k] || null, setItem: (k, v) => {data[k] = v}, removeItem: k => {delete data[k]}};
function addEventListener() {}
const document = {addEventListener() {}, hidden: false};
'''
        check = '''
if (data["snap:overview"]) throw Error("previous user's dashboard was not removed");
'''
        result = subprocess.run(["node", "-e", setup + code + check], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('localStorage.setItem("snap:', html)
        self.assertNotIn('localStorage.getItem("snap:', html)
