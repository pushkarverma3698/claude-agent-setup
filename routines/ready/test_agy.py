#!/usr/bin/env python3
"""Tests for agy (project_id resolution, reply, wait, new --wait, send --wait) and agy-guard (--conv, self-exclusion)."""
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
AGY = os.path.join(ROOT, "agy")
GUARD = os.path.join(ROOT, "agy-guard")

FAKE_AGENTAPI = """#!/usr/bin/env bash
set -eu
echo "CMD=$* PID=${ANTIGRAVITY_PROJECT_ID:-} SRC=${ANTIGRAVITY_SOURCE_METADATA:-}" >> "$AGENTAPI_LOG"
cmd="${1:-}"; shift || true
case "$cmd" in
  get-conversation-metadata)
    echo '{"response":{"conversationMetadata":{"metadata":{"projectId":"proj-founderos-123"}}}}'
    ;;
  new-conversation)
    if [ -z "${ANTIGRAVITY_PROJECT_ID:-}" ]; then
      echo '{"response":{},"error":"project_id is required when providing project_env_config"}'
      exit 0
    fi
    if [ -n "${ANTIGRAVITY_SOURCE_METADATA:-}" ]; then
      echo '{"response":{},"error":"source project_id does not match target project_id"}'
      exit 0
    fi
    echo '{"response":{"newConversation":{"conversationId":"conv-turn-1"}}}'
    ;;
  send-message)
    rec="${1:-}"; msg="${2:-}"
    echo "{\"response\":{\"sendMessage\":{\"recipientId\":\"$rec\",\"content\":\"$msg\"}}}"
    ;;
  *)
    echo "unexpected agentapi call: $cmd $*" >&2
    exit 1
    ;;
esac
"""


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class AgyAndGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = self.tmp.name
        self.brain = os.path.join(d, "brain")
        self.ledgers = os.path.join(d, "conversations")
        self.projects = os.path.join(d, "projects")
        self.repo = os.path.join(d, "Projects", "founderos")
        os.makedirs(self.brain)
        os.makedirs(self.ledgers)
        os.makedirs(self.projects)
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)

        with open(os.path.join(self.projects, "p1.json"), "w") as f:
            json.dump({
                "id": "proj-founderos-123",
                "name": "founderos",
                "projectResources": {
                    "resources": [{"gitFolder": {"folderUri": f"file://{self.repo}"}}]
                },
            }, f)

        self.agentapi = os.path.join(d, "fake-agentapi")
        with open(self.agentapi, "w") as f:
            f.write(FAKE_AGENTAPI)
        os.chmod(self.agentapi, 0o755)
        self.log = os.path.join(d, "agentapi.log")

    def tearDown(self):
        self.tmp.cleanup()

    def base_env(self):
        env = dict(os.environ)
        for k in list(env):
            if k.startswith("ANTIGRAVITY_"):
                env.pop(k)
        env.update({
            "AGY_BRAIN_DIR": self.brain,
            "AGY_LEDGER_DIR": self.ledgers,
            "AGY_PROJECTS_DIR": self.projects,
            "AGY_AGENTAPI_BIN": self.agentapi,
            "AGY_SKIP_LS_PROBE": "1",
            "AGY_ALLOW_PARALLEL": "1",
            "AGENTAPI_LOG": self.log,
        })
        return env

    def transcript_file(self, conv_id):
        return os.path.join(self.brain, conv_id, ".system_generated", "logs", "transcript_full.jsonl")

    def test_reply_returns_completed_turn_and_rejects_mid_turn(self):
        tf = self.transcript_file("conv-1")
        write_jsonl(tf, [
            {"step_index": 0, "type": "USER_INPUT", "status": "DONE", "content": "hi"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE",
             "tool_calls": [{"name": "run_command"}], "content": "running"},
        ])
        r_mid = subprocess.run([AGY, "reply", "conv-1"], env=self.base_env(), capture_output=True, text=True)
        self.assertEqual(r_mid.returncode, 2, r_mid.stderr)

        write_jsonl(tf, [
            {"step_index": 0, "type": "USER_INPUT", "status": "DONE", "content": "hi"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE",
             "tool_calls": [{"name": "run_command"}], "content": "running"},
            {"step_index": 2, "type": "GENERIC", "status": "DONE", "content": "exit=0"},
            {"step_index": 3, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "CLOSE-OUT: PASS"},
        ])
        r_ok = subprocess.run([AGY, "reply", "conv-1"], env=self.base_env(), capture_output=True, text=True)
        self.assertEqual(r_ok.returncode, 0, r_ok.stderr)
        self.assertEqual(r_ok.stdout.strip(), "CLOSE-OUT: PASS")

    def test_new_wait_resolves_project_id_and_scrubs_source_metadata(self):
        tf = self.transcript_file("conv-turn-1")

        def writer():
            time.sleep(0.4)
            write_jsonl(tf, [
                {"step_index": 0, "type": "USER_INPUT", "status": "DONE", "content": "brief"},
                {"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "TURN-1 DONE"},
            ])

        t = threading.Thread(target=writer)
        t.start()
        env = self.base_env()
        env["ANTIGRAVITY_SOURCE_METADATA"] = '{"stale":"true"}'
        r = subprocess.run(
            [AGY, "new", "--wait", "--settle=1", "--timeout=5", "--model=pro", "do task"],
            cwd=self.repo, env=env, capture_output=True, text=True
        )
        t.join()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CONVERSATION_ID=conv-turn-1", r.stdout)
        self.assertIn("TURN-1 DONE", r.stdout)
        with open(self.log) as f:
            logged = f.read()
        self.assertIn("PID=proj-founderos-123 SRC=", logged)

    def test_send_wait_waits_for_step_after_previous_turn(self):
        tf = self.transcript_file("conv-turn-1")
        write_jsonl(tf, [
            {"step_index": 0, "type": "USER_INPUT", "status": "DONE", "content": "brief"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "TURN-1 DONE"},
        ])

        def writer():
            time.sleep(0.4)
            write_jsonl(tf, [
                {"step_index": 0, "type": "USER_INPUT", "status": "DONE", "content": "brief"},
                {"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "TURN-1 DONE"},
                {"step_index": 2, "type": "SYSTEM_MESSAGE", "status": "DONE", "content": "fix blocker"},
                {"step_index": 3, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "TURN-2 FIXED"},
            ])

        t = threading.Thread(target=writer)
        t.start()
        r = subprocess.run(
            [AGY, "send", "--wait", "--settle=1", "--timeout=5", "conv-turn-1", "fix blocker"],
            cwd=self.repo, env=self.base_env(), capture_output=True, text=True
        )
        t.join()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "TURN-2 FIXED")

    def test_guard_conv_and_self_exclusion(self):
        tf = self.transcript_file("conv-self")
        db_self = os.path.join(self.ledgers, "conv-self.db")
        with open(db_self, "w") as f:
            f.write("x")
        write_jsonl(tf, [
            {"step_index": 0, "type": "USER_INPUT", "status": "DONE", "content": "x"},
        ])
        r_busy = subprocess.run([GUARD, "--conv", "conv-self", "0"], env=self.base_env(), capture_output=True, text=True)
        self.assertEqual(r_busy.returncode, 1)

        write_jsonl(tf, [
            {"step_index": 0, "type": "USER_INPUT", "status": "DONE", "content": "x"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "done"},
        ])
        r_idle = subprocess.run([GUARD, "--conv", "conv-self", "0"], env=self.base_env(), capture_output=True, text=True)
        self.assertEqual(r_idle.returncode, 0, r_idle.stdout + r_idle.stderr)

        # Self-exclusion when ANTIGRAVITY_CONVERSATION_ID is set:
        env_self = self.base_env()
        env_self["ANTIGRAVITY_CONVERSATION_ID"] = "conv-self"
        r_self = subprocess.run([GUARD, "3"], env=env_self, capture_output=True, text=True)
        self.assertEqual(r_self.returncode, 0, r_self.stdout)

        # A second active ledger still causes BUSY:
        db_other = os.path.join(self.ledgers, "conv-other.db")
        with open(db_other, "w") as f:
            f.write("y")
        r_other = subprocess.run([GUARD, "3"], env=env_self, capture_output=True, text=True)
        self.assertEqual(r_other.returncode, 1, r_other.stdout)


if __name__ == "__main__":
    unittest.main()
