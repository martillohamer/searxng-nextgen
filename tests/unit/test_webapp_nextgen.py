# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,invalid-name,protected-access

import searx
import searx.webapp
import searx.stealth
import searx.deepweb

from searx import nextgen_webui
from tests import SearxTestCase


class NextGenWebUiTestCase(SearxTestCase):

    def _enable_nextgen(self):
        """Enable Phase C + Martillohamer pillar (with stealth) and restore after."""
        ng = searx.settings["search"]["nextgen"]
        pillar = ng.setdefault("martillohamer_sear", {})
        previous = (ng.get("enabled"), pillar.get("enabled"), pillar.get("stealth_mode"))

        ng["enabled"] = True
        pillar["enabled"] = True
        pillar["stealth_mode"] = True

        def restore():
            ng["enabled"] = previous[0]
            pillar["enabled"] = previous[1]
            pillar["stealth_mode"] = previous[2]

        self.addCleanup(restore)

    #
    # gating / feature flags
    #

    def test_tools_return_404_when_disabled(self):
        for path in ("/nextgen/ghost", "/nextgen/dorks", "/nextgen/deepweb"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_toolbar_hidden_when_disabled(self):
        result = self.client.post('/')
        self.assertEqual(result.status_code, 200)
        self.assertNotIn(b'id="nextgen_toolbar"', result.data)

    def test_toolbar_visible_when_enabled(self):
        self._enable_nextgen()
        result = self.client.get('/')
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'id="nextgen_toolbar"', result.data)
        self.assertIn(b'id="nextgen_ghost"', result.data)

    def test_admin_enabled_reflects_settings(self):
        self.assertFalse(nextgen_webui.admin_enabled())
        self._enable_nextgen()
        self.assertTrue(nextgen_webui.admin_enabled())
        self.assertTrue(nextgen_webui.stealth_available())

    def test_tools_available_when_enabled(self):
        self._enable_nextgen()
        self.assertEqual(self.client.get('/nextgen/dorks').status_code, 200)
        self.assertEqual(self.client.get('/nextgen/deepweb').status_code, 200)
        # ghost toggle redirects back to the index (no referrer in tests)
        self.assertEqual(self.client.get('/nextgen/ghost').status_code, 302)

    def test_ghost_toggle_persists_preference(self):
        self._enable_nextgen()
        result = self.client.get('/nextgen/ghost')
        self.assertEqual(result.status_code, 302)
        # the cookie saved by the toggle is sent back on the next request
        result = self.client.get('/')
        self.assertIn(b'Ghost: ON', result.data)

    def test_ghost_request_activates_and_deactivates_stealth(self):
        self._enable_nextgen()
        calls = {"activate": 0, "deactivate": 0}
        real_activate = searx.stealth.activate
        real_deactivate = searx.stealth.deactivate

        def fake_activate(*args, **kwargs):
            calls["activate"] += 1
            return real_activate(*args, **kwargs)

        def fake_deactivate(*args, **kwargs):
            calls["deactivate"] += 1
            return real_deactivate(*args, **kwargs)

        self.setattr4test(searx.stealth, "activate", fake_activate)
        self.setattr4test(searx.stealth, "deactivate", fake_deactivate)

        self.client.set_cookie("ghost_mode", "True")
        result = self.client.get('/healthz')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(calls["activate"], 1)
        self.assertEqual(calls["deactivate"], 1)
        self.assertFalse(searx.stealth.is_active())

    def test_no_stealth_without_ghost_preference(self):
        self._enable_nextgen()
        calls = {"activate": 0}
        real_activate = searx.stealth.activate

        def fake_activate(*args, **kwargs):
            calls["activate"] += 1
            return real_activate(*args, **kwargs)

        self.setattr4test(searx.stealth, "activate", fake_activate)
        self.client.get('/healthz')
        self.assertEqual(calls["activate"], 0)

    def test_dorks_view_builds_dorks_and_extracts_indicators(self):
        self._enable_nextgen()
        result = self.client.get('/nextgen/dorks?q=example.com&text=mail%20admin%40example.com')
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'example.com', result.data)
        self.assertIn(b'admin@example.com', result.data)

    def test_deepweb_failsafe_when_tor_unavailable(self):
        self._enable_nextgen()
        self.setattr4test(searx.deepweb, "tor_available", lambda *a, **k: False)
        self.setattr4test(searx.deepweb, "configured_engines", lambda: ["ahmia"])
        result = self.client.get('/nextgen/deepweb?q=test')
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'Tor is not reachable', result.data)

    def test_deepweb_renders_results(self):
        self._enable_nextgen()
        self.setattr4test(searx.deepweb, "tor_available", lambda *a, **k: True)
        self.setattr4test(searx.deepweb, "configured_engines", lambda: ["ahmia"])

        def fake_search(query, *args, **kwargs):  # pylint: disable=unused-argument
            return {
                "query": query,
                "enabled": True,
                "engines": ["ahmia"],
                "results": [{"engine": "ahmia", "title": "Hidden Service", "url": "http://abcdef.onion"}],
                "result_count": 1,
                "errors": {},
                "tor_available": True,
            }

        self.setattr4test(searx.deepweb, "search", fake_search)
        result = self.client.get('/nextgen/deepweb?q=test')
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'Hidden Service', result.data)
        self.assertIn(b'http://abcdef.onion', result.data)

    def test_preferences_page_shows_nextgen_toggles_when_enabled(self):
        self._enable_nextgen()
        result = self.client.get('/preferences')
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'name="ghost_mode"', result.data)
        self.assertIn(b'name="osint_dorks"', result.data)
        self.assertIn(b'name="deepweb"', result.data)

    def test_preferences_page_hides_nextgen_toggles_when_disabled(self):
        result = self.client.get('/preferences')
        self.assertEqual(result.status_code, 200)
        self.assertNotIn(b'name="ghost_mode"', result.data)
