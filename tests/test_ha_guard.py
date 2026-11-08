import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ha_guard


class HaGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = patch.object(ha_guard, "STATE_FILE", Path(self.tmp.name) / "ha_approvals.json")
        self.state.start()

    def tearDown(self):
        self.state.stop()
        self.tmp.cleanup()

    def test_reads_and_everyday_control_run_without_a_yes(self):
        for method, path in [("GET", "/api/states"), ("POST", "/api/template"),
                             ("POST", "/api/services/light/turn_on"), ("POST", "/api/services/climate/set_temperature"),
                             ("POST", "/api/services/scene/turn_on"), ("POST", "/api/services/homeassistant/toggle"),
                             ("POST", "/api/services/homeassistant/check_config")]:
            self.assertFalse(ha_guard.needs_yes_rest(method, path), path)
        for msg in [{"type": "get_states"}, {"type": "config/entity_registry/list"}, {"type": "zha/devices"},
                    {"type": "call_service", "domain": "switch", "service": "turn_off"}]:
            self.assertFalse(ha_guard.needs_yes_ws(msg), msg)

    def test_config_deletion_restart_security_and_scripts_need_a_yes(self):
        for method, path in [("POST", "/api/config/config_entries/options/flow"), ("DELETE", "/api/config/x"),
                             ("POST", "/api/services/homeassistant/restart"), ("POST", "/api/services/lock/unlock"),
                             ("POST", "/api/services/alarm_control_panel/alarm_disarm"), ("POST", "/api/services/script/turn_on"),
                             ("POST", "/api/services/automation/trigger"), ("POST", "/api/states/light.x"),
                             ("POST", "/api/services/hassio/addon_install")]:
            self.assertTrue(ha_guard.needs_yes_rest(method, path), path)
        for msg in [{"type": "config/entity_registry/remove", "entity_id": "light.x"}, {"type": "zha/device/remove", "ieee": "1"},
                    {"type": "call_service", "domain": "lock", "service": "unlock"}]:
            self.assertTrue(ha_guard.needs_yes_ws(msg), msg)

    def test_a_yes_lets_the_answering_turn_make_its_changes(self):
        """R2 asks first, the owner says yes: the turn that answers may make up to
        YES_WINDOW_CALLS changes, then needs a new yes."""
        call = lambda n: {"api": "rest", "method": "POST", "path": "/api/services/script/turn_on", "body": {"entity_id": f"script.{n}"}}
        self.assertIsNotNone(ha_guard.check(call(0), "before any yes"))
        ha_guard.on_user_message("r2", "yes, go ahead")
        for n in range(ha_guard.YES_WINDOW_CALLS):
            self.assertIsNone(ha_guard.check(call(n + 1), "x"), n)
        self.assertIsNotNone(ha_guard.check(call(99), "x"), "the window holds a few calls, not unlimited")

    def test_a_yes_covers_the_whole_described_batch_of_2026_10_13(self):
        """R2's flag: entity renames + a dashboard, refused partway after a yes,
        leaving HA half-migrated. The answering turn may now finish the batch."""
        rename = lambda n: {"api": "ws", "message": {"type": "config/entity_registry/update",
                                                     "entity_id": f"sensor.old_{n}", "new_entity_id": f"sensor.new_{n}"}}
        dashboard = {"api": "ws", "message": {"type": "lovelace/config/save", "config": {}}}
        self.assertIsNotNone(ha_guard.check(rename(0), "asks first"))
        ha_guard.on_user_message("r2", "yes")
        for n in range(14):
            self.assertIsNone(ha_guard.check(rename(n), "rename"), n)
        self.assertIsNone(ha_guard.check(dashboard, "dashboard"))
        # The turn ends: the next turn needs a new yes.
        ha_guard.end_turn("r2")
        self.assertIsNotNone(ha_guard.check(rename(99), "later"))

    def test_a_runaway_loop_stops_and_says_why(self):
        call = lambda n: {"api": "rest", "method": "DELETE", "path": f"/api/config/x{n}", "body": None}
        ha_guard.on_user_message("r2", "yes")
        for n in range(ha_guard.YES_WINDOW_CALLS):
            self.assertIsNone(ha_guard.check(call(n), "x"))
        for n in (100, 101):  # still says why on the next tries
            self.assertIn(f"already made {ha_guard.YES_WINDOW_CALLS} confirmed changes", ha_guard.check(call(n), "x"))
        ha_guard.end_turn("r2")
        self.assertIn("must confirm this first", ha_guard.check(call(102), "x"))

    def test_r2_dashboard_loop_of_2026_10_04(self):
        """Read the dashboard (free), save it (needs yes), the owner says yes, R2
        reads again and saves a rebuilt config: goes through."""
        self.assertFalse(ha_guard.needs_yes_ws({"type": "lovelace/config"}), "reading a dashboard is a read")
        save = lambda cards: {"api": "ws", "message": {"type": "lovelace/config/save", "config": {"views": [{"cards": cards}]}}}
        self.assertTrue(ha_guard.needs_yes_ws(save([1])["message"]))
        self.assertIsNotNone(ha_guard.check(save([1, 2]), "WS lovelace/config/save"))
        self.assertEqual(ha_guard.on_user_message("r2", "yes"), 1)
        self.assertIsNone(ha_guard.check(save([1, 2, 3]), "WS lovelace/config/save"), "same action, details rebuilt")

    def test_ws_commands_with_a_change_verb_need_a_yes(self):
        for t in ("config/entity_registry/update", "zha/devices/permit", "conversation/process", "auth/delete_refresh_token", "zha/network/change_channel"):
            self.assertTrue(ha_guard.needs_yes_ws({"type": t}), t)
        for t in ("get_states", "config/device_registry/list", "recorder/list_statistic_ids", "lovelace/config", "zha/network/settings"):
            self.assertFalse(ha_guard.needs_yes_ws({"type": t}), t)

    def test_anything_but_yes_closes_the_window(self):
        call = {"api": "ws", "message": {"type": "config/entity_registry/remove", "entity_id": "light.x"}}
        ha_guard.on_user_message("r2", "yes")
        ha_guard.on_user_message("r2", "hmm, what would that remove exactly?")
        self.assertIsNotNone(ha_guard.check(call, "remove"), "a question isn't a yes")

    def test_the_window_expires(self):
        call = {"api": "rest", "method": "DELETE", "path": "/api/config/x", "body": None}
        ha_guard.on_user_message("r2", "ja")
        later = ha_guard.time.time() + ha_guard.YES_WINDOW_S + 1
        with patch.object(ha_guard.time, "time", return_value=later):
            self.assertIsNotNone(ha_guard.check(call, "delete"))

    def test_zha_backup_evening_of_2026_10_04(self):
        """'Want me to make the ZHA backup now?' 'yes': goes through, even
        after R2 corrects the command name in the same turn."""
        self.assertFalse(ha_guard.needs_yes_ws({"type": "zha/network/settings"}), "reading ZHA settings is a read")
        ha_guard.on_user_message("r2", "yes")
        self.assertIsNone(ha_guard.check({"api": "ws", "message": {"type": "zha/network/backup/create"}}, "typo"))
        self.assertIsNone(ha_guard.check({"api": "ws", "message": {"type": "zha/network/backups/create"}}, "fixed"))

    def test_other_agents_never_approve(self):
        call = {"api": "rest", "method": "DELETE", "path": "/api/config/x", "body": None}
        ha_guard.check(call, "delete")
        self.assertEqual(ha_guard.on_user_message("reba", "yes"), 0)
        self.assertEqual(ha_guard.on_user_message("r2", "ja"), 1, "Norwegian yes works")

    def _refused_id(self, call, conversation):
        ha_guard.on_owner_message("r2", "please clean up the old helper", conversation)
        refusal = ha_guard.check(call, "remove input_boolean.old")
        return refusal.split("approval id ", 1)[1][:4]

    def test_approve_by_id_from_another_chat_runs_exactly_that_call_once(self):
        remove = {"api": "ws", "message": {"type": "config/entity_registry/remove", "entity_id": "input_boolean.old"}}
        rid = self._refused_id(remove, "chat-a")
        ha_guard.end_turn("r2")
        approved, note = ha_guard.on_owner_message("r2", f"I approve {rid.lower()}", "chat-b")
        self.assertEqual(approved, 1)
        self.assertIn(f"approved request {rid}", note)
        self.assertIn("input_boolean.old", note)
        other = {"api": "ws", "message": {"type": "config/entity_registry/remove", "entity_id": "light.kitchen"}}
        self.assertIsNotNone(ha_guard.check(other, "something else"), "only the approved action")
        self.assertIsNone(ha_guard.check(remove, "the approved one"))
        self.assertIsNotNone(ha_guard.check(remove, "again"), "once")
        self.assertEqual(ha_guard.requests("approved")[0]["id"], rid)
        _, note = ha_guard.on_owner_message("r2", f"approve {rid}", "chat-b")
        self.assertIn("already approved", note)

    def test_unknown_and_expired_ids_are_refused_with_the_reason(self):
        _, note = ha_guard.on_owner_message("r2", "approve ZZZZ", "chat-a")
        self.assertIn("no approval request ZZZZ", note)
        rid = self._refused_id({"api": "rest", "method": "DELETE", "path": "/api/config/x", "body": None}, "chat-a")
        later = ha_guard.time.time() + ha_guard.REQUEST_TTL_S + 1
        with patch.object(ha_guard.time, "time", return_value=later):
            approved, note = ha_guard.on_owner_message("r2", f"approve {rid}", "chat-a")
        self.assertEqual(approved, 0)
        self.assertIn("expired", note)

    def test_a_plain_yes_in_another_chat_does_not_approve(self):
        call = {"api": "rest", "method": "DELETE", "path": "/api/config/x", "body": None}
        self._refused_id(call, "chat-a")
        ha_guard.end_turn("r2")
        ha_guard.on_owner_message("r2", "yes", "chat-b")
        ha_guard.end_turn("r2")
        self.assertIsNotNone(ha_guard.check(call, "delete"), "chat-b's yes was not about chat-a's request")

    def test_a_plain_yes_only_answers_the_question_just_asked(self):
        call = {"api": "rest", "method": "DELETE", "path": "/api/config/x", "body": None}
        self._refused_id(call, "chat-a")
        ha_guard.end_turn("r2")
        ha_guard.on_owner_message("r2", "what else is in there?", "chat-a")
        ha_guard.end_turn("r2")
        self.assertEqual(ha_guard.on_owner_message("r2", "yes", "chat-a")[0], 0, "a later yes needs the id")

    def test_refused_call_never_reaches_home_assistant(self):
        with patch.dict(os.environ, {"HA_API_TOKEN": "test"}):
            import mcp_ha_server
        with patch.object(mcp_ha_server.urllib.request, "urlopen") as opened:
            reply = mcp_ha_server.ha_api("POST", "/api/services/lock/unlock", {"entity_id": "lock.front"})
        opened.assert_not_called()
        self.assertIn("NOT DONE", reply)


if __name__ == "__main__":
    unittest.main()
