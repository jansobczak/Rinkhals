import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "files/4-apps/home/rinkhals/apps/40-moonraker/mmu_ace.py"
)
MODULE_NAME = "testsupport.moonraker.components.mmu_ace"
COMMON_NAME = "testsupport.moonraker.common"


class DummyTask:
    def __init__(self, coro):
        self._coro = coro
        coro.close()

    def cancel(self):
        return None

    def done(self):
        return True


class DummyEventLoop:
    def create_task(self, coro):
        return DummyTask(coro)


class DummyKlippyApis:
    async def query_objects(self, *args, **kwargs):
        return {}


class DummyKlippyConnection:
    async def request(self, *args, **kwargs):
        return {}


class DummyDatabase:
    """Minimal stand-in for Moonraker's database component: get_item(namespace,
    key, default) reads from a flat {(namespace, key): value} map, matching the
    real component's dotted-path key convention (e.g. "uiSettings.general.instanceName")."""

    def __init__(self, items=None):
        self._items = dict(items or {})

    async def get_item(self, namespace, key=None, default=None):
        return self._items.get((namespace, key), default)


class DummyServer:
    error = RuntimeError

    def __init__(self, hostname="Rockchip"):
        self.events = []
        self._eventloop = DummyEventLoop()
        self._hostname = hostname
        self._components = {
            "klippy_apis": DummyKlippyApis(),
            "klippy_connection": DummyKlippyConnection(),
            "database": DummyDatabase(),
        }
        self._data_path = tempfile.mkdtemp()
        Path(self._data_path, "config").mkdir(parents=True, exist_ok=True)

    def get_event_loop(self):
        return self._eventloop

    def lookup_component(self, name, default=None):
        return self._components.get(name, default)

    def get_app_args(self):
        return {"data_path": self._data_path}

    def send_event(self, name, payload):
        self.events.append((name, payload))

    def get_host_info(self):
        return {"hostname": self._hostname}


def _ensure_package(name: str):
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = []
        sys.modules[name] = package
    return sys.modules[name]


def load_mmu_ace_module():
    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]

    _ensure_package("testsupport")
    _ensure_package("testsupport.moonraker")
    _ensure_package("testsupport.moonraker.components")

    common_module = types.ModuleType(COMMON_NAME)
    common_module.WebRequest = type("WebRequest", (), {})
    common_module.APITransport = type("APITransport", (), {})
    common_module.RequestType = type("RequestType", (), {})
    sys.modules[COMMON_NAME] = common_module

    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MmuAceControllerSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_mmu_ace_module()

    def setUp(self):
        self.original_create_task = self.module.asyncio.create_task
        self.module.asyncio.create_task = lambda coro: DummyTask(coro)
        self.server = DummyServer()
        self.controller = self.module.MmuAceController(self.server, host=None)
        self.controller.ace = self.module.MmuAce()
        self.controller._handle_status_update = lambda *args, **kwargs: None

    def tearDown(self):
        self.module.asyncio.create_task = self.original_create_task

    def _build_filament_hub(self, current_filament: str):
        return {
            "current_filament": current_filament,
            "filament_hubs": [
                {
                    "id": 0,
                    "status": "ready",
                    "temp": 25,
                    "slots": [
                        {"index": 0, "status": "empty", "sku": "", "type": "", "color": [0, 0, 0], "rfid": 1, "source": 3},
                        {"index": 1, "status": "ready", "sku": "", "type": "PLA", "color": [255, 255, 255], "rfid": 1, "source": 2},
                        {"index": 2, "status": "empty", "sku": "", "type": "", "color": [0, 0, 0], "rfid": 1, "source": 3},
                        {"index": 3, "status": "empty", "sku": "", "type": "", "color": [0, 0, 0], "rfid": 1, "source": 3},
                    ],
                }
            ],
        }

    def test_refresh_syncs_loaded_gate_and_widget_state(self):
        self.controller._set_ace_status(self._build_filament_hub("0-1"))

        self.assertEqual(self.controller.ace.loaded_gate, 1)
        self.assertEqual(self.controller.ace.gate, 1)
        self.assertEqual(self.controller.ace.tool, 1)
        self.assertEqual(self.controller.ace.filament.pos, self.module.FILAMENT_POS_LOADED)

        status = self.controller.get_status()
        self.assertEqual(status.mmu.gate, 1)
        self.assertEqual(status.mmu.tool, 1)
        self.assertEqual(status.mmu.filament_pos, self.module.FILAMENT_POS_LOADED)
        self.assertFalse(status.mmu.active_filament.empty)

    def test_refresh_clears_loaded_gate_when_hardware_reports_empty(self):
        self.controller.ace.loaded_gate = 1
        self.controller.ace.gate = 1
        self.controller.ace.tool = 1
        self.controller.ace.filament.pos = self.module.FILAMENT_POS_LOADED

        self.controller._set_ace_status(self._build_filament_hub(""))

        self.assertEqual(self.controller.ace.loaded_gate, self.module.TOOL_GATE_UNKNOWN)
        self.assertEqual(self.controller.ace.gate, self.module.TOOL_GATE_UNKNOWN)
        self.assertEqual(self.controller.ace.tool, self.module.TOOL_GATE_UNKNOWN)
        self.assertEqual(self.controller.ace.filament.pos, self.module.FILAMENT_POS_UNLOADED)

        status = self.controller.get_status()
        self.assertEqual(status.mmu.filament_pos, self.module.FILAMENT_POS_UNLOADED)
        self.assertTrue(status.mmu.active_filament.empty)


class DummySpoolman:
    def __init__(self, sync_rate_seconds=5):
        self.calls = []
        self.spoolman_url = "http://spoolman.test/api"
        self.sync_rate_seconds = sync_rate_seconds

    def set_active_spool(self, spool_id):
        self.calls.append(spool_id)


class DummyHttpResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        # NOTE: must check "is not None", not "body or {}" - an intentionally
        # empty list body (e.g. "Spoolman has zero spools") is falsy and would
        # otherwise silently be coerced into {} by `or`.
        self._body = body if body is not None else {}

    def has_error(self):
        return self.status_code >= 400

    def json(self):
        return self._body


class DummyHttpClient:
    """response/exception give one fixed answer to every call (original,
    still-supported behavior). responses additionally allows different
    answers per exact (method, url) pair, needed once a single test exercises
    more than one endpoint (e.g. a GET to fetch a spool followed by a PATCH
    to link it)."""

    def __init__(self, response=None, exception=None, responses=None):
        self.response = response
        self.exception = exception
        self.responses = dict(responses or {})
        self.requested_urls = []
        self.requests = []  # [(method, url, body)]

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def request(self, method, url, body=None, **kwargs):
        method = method.upper()
        self.requested_urls.append(url)
        self.requests.append((method, url, body))
        if self.exception is not None:
            raise self.exception
        if (method, url) in self.responses:
            return self.responses[(method, url)]
        return self.response


class MmuAceGatePrecedenceTests(unittest.IsolatedAsyncioTestCase):
    """A gate's RFID base data, a local manual/Spoolman override, and a Spoolman
    pull-cache entry can all exist at once. Precedence: override > pull cache >
    RFID/defaults - resolved per gate, every poll, with no global mode switch."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_mmu_ace_module()

    def setUp(self):
        self.original_create_task = self.module.asyncio.create_task
        self.module.asyncio.create_task = lambda coro: DummyTask(coro)
        self.server = DummyServer()
        self.spoolman = DummySpoolman()
        self.server._components["spoolman"] = self.spoolman

    def tearDown(self):
        self.module.asyncio.create_task = self.original_create_task

    def _make_controller(self, spoolman_support: str = "off"):
        controller = self.module.MmuAceController(
            self.server, host=None, spoolman_support=spoolman_support
        )
        controller.ace = self.module.MmuAce()
        controller._handle_status_update = lambda *args, **kwargs: None
        return controller

    def _build_filament_hub_gate2(self, sku: str, gate_type: str, rfid: int = 1):
        return {
            "current_filament": "",
            "filament_hubs": [
                {
                    "id": 0,
                    "status": "ready",
                    "temp": 25,
                    "slots": [
                        {"index": 0, "status": "empty", "sku": "", "type": "", "color": [0, 0, 0], "rfid": 1, "source": 3},
                        {"index": 1, "status": "empty", "sku": "", "type": "", "color": [0, 0, 0], "rfid": 1, "source": 3},
                        {"index": 2, "status": "ready", "sku": sku, "type": gate_type, "color": [255, 255, 255], "rfid": rfid, "source": 2},
                        {"index": 3, "status": "empty", "sku": "", "type": "", "color": [0, 0, 0], "rfid": 1, "source": 3},
                    ],
                }
            ],
        }

    def test_untouched_rfid_gate_never_has_a_fabricated_spool_id(self):
        # Old behavior parsed the SKU's own serial number (e.g. the "107" in
        # AHPLBW-107) as a fake spool_id, risking collision with an unrelated
        # real Spoolman spool sharing that number. Deleted entirely, not
        # replaced - an untouched RFID gate always reports -1.
        controller = self._make_controller()

        controller._set_ace_status(self._build_filament_hub_gate2("AHPLBW-107", "PLA"))

        _, gate = controller._get_gate_by_index(2)
        self.assertEqual(gate.spool_id, -1)
        self.assertEqual(gate.material, "PLA")

    def test_local_override_takes_priority_over_rfid_data(self):
        controller = self._make_controller()
        controller._gate_spool_overrides = {2: {"spool_id": 55, "material": "PETG", "sku_at_link": ""}}

        controller._set_ace_status(self._build_filament_hub_gate2("AHPLBW-107", "PLA"))

        _, gate = controller._get_gate_by_index(2)
        self.assertEqual(gate.spool_id, 55)
        self.assertEqual(gate.material, "PETG")

    def test_override_auto_clears_when_tag_reports_a_different_sku(self):
        # Physical spool swapped without updating the gate mapping - the tag
        # now disagrees with what the override was linked against, so RFID
        # takes back over rather than keep showing the stale linked spool.
        controller = self._make_controller()
        controller._gate_spool_overrides = {2: {"spool_id": 55, "material": "PETG", "sku_at_link": "AHPLBW-107"}}

        controller._set_ace_status(self._build_filament_hub_gate2("AHPLDB-203", "PLA"))

        _, gate = controller._get_gate_by_index(2)
        self.assertNotIn(2, controller._gate_spool_overrides)
        self.assertEqual(gate.spool_id, -1)
        self.assertEqual(gate.material, "PLA")

    def test_override_survives_when_tag_sku_is_unchanged(self):
        controller = self._make_controller()
        controller._gate_spool_overrides = {2: {"spool_id": 55, "material": "PETG", "sku_at_link": "AHPLBW-107"}}

        controller._set_ace_status(self._build_filament_hub_gate2("AHPLBW-107", "PLA"))

        _, gate = controller._get_gate_by_index(2)
        self.assertIn(2, controller._gate_spool_overrides)
        self.assertEqual(gate.spool_id, 55)
        self.assertEqual(gate.material, "PETG")

    def test_pull_cache_applies_when_no_local_override(self):
        controller = self._make_controller(spoolman_support="pull")
        controller._spoolman_pull_cache = {2: {"spool_id": 77, "material": "ABS", "filament_name": "Pulled ABS"}}

        controller._set_ace_status(self._build_filament_hub_gate2("", ""))

        _, gate = controller._get_gate_by_index(2)
        self.assertEqual(gate.spool_id, 77)
        self.assertEqual(gate.material, "ABS")
        self.assertEqual(gate.filament_name, "Pulled ABS")

    def test_local_override_takes_priority_over_pull_cache(self):
        controller = self._make_controller(spoolman_support="pull")
        controller._gate_spool_overrides = {2: {"spool_id": 55, "material": "PETG", "sku_at_link": ""}}
        controller._spoolman_pull_cache = {2: {"spool_id": 77, "material": "ABS"}}

        controller._set_ace_status(self._build_filament_hub_gate2("", ""))

        _, gate = controller._get_gate_by_index(2)
        self.assertEqual(gate.spool_id, 55)
        self.assertEqual(gate.material, "PETG")


class MmuAceUpdateGateTests(unittest.IsolatedAsyncioTestCase):
    """update_gate() is always allowed now (no RFID rejection), always persists
    its full record (even without a linked spool_id), and pulls filament
    attributes from Spoolman when a real spool_id is linked."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_mmu_ace_module()

    def setUp(self):
        self.original_create_task = self.module.asyncio.create_task
        self.module.asyncio.create_task = lambda coro: DummyTask(coro)
        self.server = DummyServer()
        self.spoolman = DummySpoolman()
        self.server._components["spoolman"] = self.spoolman

    def tearDown(self):
        self.module.asyncio.create_task = self.original_create_task

    def _make_controller(self, spoolman_support: str = "off"):
        controller = self.module.MmuAceController(
            self.server, host=None, spoolman_support=spoolman_support
        )
        controller.ace = self.module.MmuAce()
        controller._handle_status_update = lambda *args, **kwargs: None
        return controller

    def _build_filament_hub_gate2(self, sku: str, gate_type: str, rfid: int = 1):
        return {
            "current_filament": "",
            "filament_hubs": [
                {
                    "id": 0,
                    "status": "ready",
                    "temp": 25,
                    "slots": [
                        {"index": 0, "status": "empty", "sku": "", "type": "", "color": [0, 0, 0], "rfid": 1, "source": 3},
                        {"index": 1, "status": "empty", "sku": "", "type": "", "color": [0, 0, 0], "rfid": 1, "source": 3},
                        {"index": 2, "status": "ready", "sku": sku, "type": gate_type, "color": [255, 255, 255], "rfid": rfid, "source": 2},
                        {"index": 3, "status": "empty", "sku": "", "type": "", "color": [0, 0, 0], "rfid": 1, "source": 3},
                    ],
                }
            ],
        }

    async def test_update_gate_allowed_even_with_a_physical_tag(self):
        controller = self._make_controller()
        controller._set_ace_status(self._build_filament_hub_gate2("AHPLBW-107", "PLA", rfid=2))  # tag present

        await controller.update_gate(2, spool_id=55)

        self.assertEqual(controller._gate_spool_overrides.get(2, {}).get("spool_id"), 55)
        _, gate = controller._get_gate_by_index(2)
        self.assertEqual(gate.spool_id, 55)

    async def test_update_gate_persists_a_bare_edit_without_a_spool_id(self):
        # Previously only linked spools (spool_id > 0) persisted past the next
        # poll; a bare material/name edit would silently revert. Now any edit
        # persists.
        controller = self._make_controller()
        controller._set_ace_status(self._build_filament_hub_gate2("", ""))

        await controller.update_gate(2, material="PETG", filament_name="My PETG")
        controller._set_ace_status(self._build_filament_hub_gate2("", ""))  # simulate next poll

        _, gate = controller._get_gate_by_index(2)
        self.assertEqual(gate.material, "PETG")
        self.assertEqual(gate.filament_name, "My PETG")

    async def test_update_gate_pulls_material_and_name_from_spoolman(self):
        controller = self._make_controller()
        controller._set_ace_status(self._build_filament_hub_gate2("", ""))
        self.server._components["http_client"] = DummyHttpClient(response=DummyHttpResponse(body={
            "filament": {
                "name": "HT-PLA-GF White",
                "vendor": {"name": "PolyMaker"},
                "material": "PLA-GF",
                "settings_extruder_temp": 210,
                "color_hex": "f3f3f1",
            }
        }))

        await controller.update_gate(0, spool_id=104, material="Unknown", filament_name="Unknown")

        _, gate = controller._get_gate_by_index(0)
        self.assertEqual(gate.material, "PLA-GF")
        self.assertEqual(gate.filament_name, "HT-PLA-GF White")
        self.assertEqual(gate.vendor, "PolyMaker")
        self.assertEqual(gate.temperature, 210)
        self.assertEqual(gate.color, [243, 243, 241, 255])
        self.assertEqual(gate.spool_id, 104)

    async def test_pulled_spoolman_data_survives_the_next_status_poll(self):
        # Regression: _set_ace_status rebuilds gate objects from scratch on every
        # ACE status update, so the material/name/color/temp/vendor pulled from
        # Spoolman in update_gate() must be persisted and restored on every poll -
        # not just spool_id - or they revert to "Unknown" the moment the next
        # hardware status update arrives.
        controller = self._make_controller()
        controller._set_ace_status(self._build_filament_hub_gate2("", ""))
        self.server._components["http_client"] = DummyHttpClient(response=DummyHttpResponse(body={
            "filament": {
                "name": "HT-PLA-GF White",
                "vendor": {"name": "PolyMaker"},
                "material": "PLA-GF",
                "settings_extruder_temp": 210,
                "color_hex": "f3f3f1",
            }
        }))
        await controller.update_gate(0, spool_id=104)

        # Simulate the next ACE hardware status update arriving.
        controller._set_ace_status(self._build_filament_hub_gate2("", ""))

        _, gate = controller._get_gate_by_index(0)
        self.assertEqual(gate.spool_id, 104)
        self.assertEqual(gate.material, "PLA-GF")
        self.assertEqual(gate.filament_name, "HT-PLA-GF White")
        self.assertEqual(gate.vendor, "PolyMaker")
        self.assertEqual(gate.temperature, 210)
        self.assertEqual(gate.color, [243, 243, 241, 255])

    async def test_update_gate_falls_back_to_local_values_when_spoolman_lookup_fails(self):
        controller = self._make_controller()
        controller._set_ace_status(self._build_filament_hub_gate2("", ""))
        self.server._components["http_client"] = DummyHttpClient(response=DummyHttpResponse(status_code=404))

        await controller.update_gate(0, spool_id=999, material="ABS", filament_name="My Custom ABS")

        _, gate = controller._get_gate_by_index(0)
        self.assertEqual(gate.material, "ABS")
        self.assertEqual(gate.filament_name, "My Custom ABS")
        self.assertEqual(gate.spool_id, 999)


class MmuAceSpoolmanActiveSpoolPushTests(unittest.IsolatedAsyncioTestCase):
    """_update_spoolman_active_spool sets Spoolman's *active* spool (usage
    tracking) whenever the loaded gate changes - independent of the
    gate-assignment push/pull mechanism below. Only needs a spool_id already
    resolved on the gate, from whatever source (override or pull cache)."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_mmu_ace_module()

    def setUp(self):
        self.original_create_task = self.module.asyncio.create_task
        self.module.asyncio.create_task = lambda coro: DummyTask(coro)
        self.server = DummyServer()
        self.spoolman = DummySpoolman()
        self.server._components["spoolman"] = self.spoolman

    def tearDown(self):
        self.module.asyncio.create_task = self.original_create_task

    def _make_controller(self, spoolman_support: str):
        controller = self.module.MmuAceController(
            self.server, host=None, spoolman_support=spoolman_support
        )
        controller.ace = self.module.MmuAce()
        controller._handle_status_update = lambda *args, **kwargs: None
        return controller

    def _build_minimal_hub(self):
        return {
            "current_filament": "",
            "filament_hubs": [
                {
                    "id": 0, "status": "ready", "temp": 25,
                    "slots": [
                        {"index": 0, "status": "empty", "sku": "", "type": "", "color": [0, 0, 0], "rfid": 1, "source": 3},
                    ],
                }
            ],
        }

    async def test_push_mode_activates_spool(self):
        controller = self._make_controller("push")
        controller._set_ace_status(self._build_minimal_hub())
        _, gate = controller._get_gate_by_index(0)
        gate.spool_id = 55

        await controller._update_spoolman_active_spool(0)

        self.assertEqual(self.spoolman.calls, [55])

    async def test_pull_mode_activates_spool_too(self):
        # Happy Hare's own reference table has pull activating the spool just
        # like push does - easy to miss since only the RFID-mode check was
        # removed here, but the mode-check itself also needed broadening.
        controller = self._make_controller("pull")
        controller._set_ace_status(self._build_minimal_hub())
        _, gate = controller._get_gate_by_index(0)
        gate.spool_id = 55

        await controller._update_spoolman_active_spool(0)

        self.assertEqual(self.spoolman.calls, [55])

    async def test_off_mode_never_activates_spool(self):
        controller = self._make_controller("off")
        controller._set_ace_status(self._build_minimal_hub())
        _, gate = controller._get_gate_by_index(0)
        gate.spool_id = 55

        await controller._update_spoolman_active_spool(0)

        self.assertEqual(self.spoolman.calls, [])

    async def test_unlinked_gate_never_activates_spool(self):
        controller = self._make_controller("push")
        controller._set_ace_status(self._build_minimal_hub())
        _, gate = controller._get_gate_by_index(0)
        gate.spool_id = -1

        await controller._update_spoolman_active_spool(0)

        self.assertEqual(self.spoolman.calls, [])


class MmuAceSpoolmanGateAssignmentSyncTests(unittest.IsolatedAsyncioTestCase):
    """Real Happy-Hare-style gate-assignment sync: push writes this printer's
    local gate->spool_id links out to Spoolman's own extra fields (+ a
    human-readable location string); pull reads them back and treats Spoolman
    as authoritative. Distinct from the active-spool push above."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_mmu_ace_module()

    def setUp(self):
        self.original_create_task = self.module.asyncio.create_task
        self.module.asyncio.create_task = lambda coro: DummyTask(coro)
        self.server = DummyServer()
        self.spoolman = DummySpoolman()
        self.server._components["spoolman"] = self.spoolman

    def tearDown(self):
        self.module.asyncio.create_task = self.original_create_task

    def _make_controller(self, spoolman_support: str, printer_name="Kobra S1 Max"):
        controller = self.module.MmuAceController(
            self.server, host=None, spoolman_support=spoolman_support, printer_name=printer_name
        )
        controller.ace = self.module.MmuAce()
        controller._handle_status_update = lambda *args, **kwargs: None
        # Bypass the extras bootstrap's own network calls (version check, field
        # creation) - covered separately in MmuAceSpoolmanExtrasBootstrapTests -
        # so these tests isolate the write/read logic itself.
        controller._spoolman_extras_ready = True
        controller._printer_name = printer_name
        return controller

    def _build_hub_gate0(self):
        return {
            "current_filament": "",
            "filament_hubs": [
                {
                    "id": 0, "status": "ready", "temp": 25,
                    "slots": [
                        {"index": 0, "status": "empty", "sku": "", "type": "", "color": [0, 0, 0], "rfid": 1, "source": 3},
                    ],
                }
            ],
        }

    async def test_update_gate_writes_gate_assignment_to_spoolman_when_push_enabled(self):
        controller = self._make_controller("push")
        controller._set_ace_status(self._build_hub_gate0())
        spool_url = f"{self.spoolman.spoolman_url}/v1/spool/104"
        http_client = DummyHttpClient(responses={
            ("GET", spool_url): DummyHttpResponse(body={"filament": {"name": "X", "material": "PLA"}}),
            ("PATCH", spool_url): DummyHttpResponse(body={}),
        })
        self.server._components["http_client"] = http_client

        await controller.update_gate(0, spool_id=104)

        patches = [r for r in http_client.requests if r[0] == "PATCH"]
        self.assertEqual(len(patches), 1)
        _, url, body = patches[0]
        self.assertEqual(url, spool_url)
        self.assertEqual(json.loads(body["extra"]["printer_name"]), "Kobra S1 Max")
        self.assertEqual(json.loads(body["extra"]["mmu_gate_map"]), 0)
        self.assertEqual(body["location"], "Kobra S1 Max @ MMU Gate:0")

    async def test_update_gate_skips_spoolman_write_when_support_is_off(self):
        controller = self._make_controller("off")
        controller._set_ace_status(self._build_hub_gate0())
        spool_url = f"{self.spoolman.spoolman_url}/v1/spool/104"
        http_client = DummyHttpClient(responses={
            ("GET", spool_url): DummyHttpResponse(body={"filament": {"name": "X", "material": "PLA"}}),
        })
        self.server._components["http_client"] = http_client

        await controller.update_gate(0, spool_id=104)

        self.assertEqual([r for r in http_client.requests if r[0] == "PATCH"], [])

    async def test_update_gate_unsets_previous_spool_when_relinked_to_none(self):
        controller = self._make_controller("push")
        controller._set_ace_status(self._build_hub_gate0())
        old_url = f"{self.spoolman.spoolman_url}/v1/spool/104"
        http_client = DummyHttpClient(responses={
            ("GET", old_url): DummyHttpResponse(body={"filament": {"name": "X", "material": "PLA"}}),
            ("PATCH", old_url): DummyHttpResponse(body={}),
        })
        self.server._components["http_client"] = http_client
        await controller.update_gate(0, spool_id=104)  # link first
        http_client.requests.clear()

        await controller.update_gate(0, spool_id=-1)  # then unlink

        patches = [r for r in http_client.requests if r[0] == "PATCH"]
        self.assertEqual(len(patches), 1)
        _, url, body = patches[0]
        self.assertEqual(url, old_url)
        self.assertEqual(json.loads(body["extra"]["printer_name"]), "")
        self.assertEqual(json.loads(body["extra"]["mmu_gate_map"]), -1)
        self.assertEqual(body["location"], "")

    async def test_refresh_pull_cache_filters_by_printer_name_and_valid_gate(self):
        controller = self._make_controller("pull")
        spool_list_url = f"{self.spoolman.spoolman_url}/v1/spool"
        records = [
            {  # matches this printer, valid gate -> included
                "id": 10, "extra": {"printer_name": json.dumps("Kobra S1 Max"), "mmu_gate_map": json.dumps(2)},
                "filament": {"name": "Match", "material": "PLA"},
            },
            {  # different printer -> excluded
                "id": 11, "extra": {"printer_name": json.dumps("Some Other Printer"), "mmu_gate_map": json.dumps(0)},
                "filament": {"name": "Other", "material": "PLA"},
            },
            {  # this printer but never actually assigned to a gate -> excluded
                "id": 12, "extra": {"printer_name": json.dumps("Kobra S1 Max"), "mmu_gate_map": json.dumps(-1)},
                "filament": {"name": "Unassigned", "material": "PLA"},
            },
        ]
        http_client = DummyHttpClient(responses={("GET", spool_list_url): DummyHttpResponse(body=records)})
        self.server._components["http_client"] = http_client

        await controller._refresh_spoolman_pull_cache()

        self.assertEqual(set(controller._spoolman_pull_cache.keys()), {2})
        self.assertEqual(controller._spoolman_pull_cache[2]["spool_id"], 10)
        self.assertEqual(controller._spoolman_pull_cache[2]["material"], "PLA")

    async def test_refresh_pull_cache_tolerates_malformed_extra_data(self):
        controller = self._make_controller("pull")
        spool_list_url = f"{self.spoolman.spoolman_url}/v1/spool"
        records = [
            {"id": 20, "extra": {"printer_name": "not-json", "mmu_gate_map": "also-not-an-int"}, "filament": {}},
        ]
        http_client = DummyHttpClient(responses={("GET", spool_list_url): DummyHttpResponse(body=records)})
        self.server._components["http_client"] = http_client

        await controller._refresh_spoolman_pull_cache()  # must not raise

        self.assertEqual(controller._spoolman_pull_cache, {})

    async def test_refresh_pull_cache_survives_a_failed_refresh(self):
        controller = self._make_controller("pull")
        controller._spoolman_pull_cache = {0: {"spool_id": 42, "material": "PLA"}}
        self.server._components["http_client"] = DummyHttpClient(exception=RuntimeError("network down"))

        await controller._refresh_spoolman_pull_cache()

        self.assertEqual(controller._spoolman_pull_cache, {0: {"spool_id": 42, "material": "PLA"}})


class MmuAceSpoolmanExtrasBootstrapTests(unittest.IsolatedAsyncioTestCase):
    """_ensure_spoolman_extras: lazy, best-effort bootstrap of the Spoolman
    extra fields (printer_name, mmu_gate_map) needed for gate-assignment
    push/pull. Never blocks startup; safe to call repeatedly."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_mmu_ace_module()

    def setUp(self):
        self.original_create_task = self.module.asyncio.create_task
        self.module.asyncio.create_task = lambda coro: DummyTask(coro)
        self.server = DummyServer()
        self.spoolman = DummySpoolman()
        self.server._components["spoolman"] = self.spoolman

    def tearDown(self):
        self.module.asyncio.create_task = self.original_create_task

    def _make_controller(self):
        controller = self.module.MmuAceController(
            self.server, host=None, spoolman_support="push", printer_name="Kobra S1 Max"
        )
        controller.ace = self.module.MmuAce()
        controller._handle_status_update = lambda *args, **kwargs: None
        return controller

    def _urls(self):
        base = self.spoolman.spoolman_url
        return base + "/v1/info", base + "/v1/field/spool"

    async def test_creates_missing_extra_fields(self):
        controller = self._make_controller()
        info_url, fields_url = self._urls()
        http_client = DummyHttpClient(responses={
            ("GET", info_url): DummyHttpResponse(body={"version": "0.19.0"}),
            ("GET", fields_url): DummyHttpResponse(body=[]),  # no fields exist yet
            ("POST", f"{fields_url}/printer_name"): DummyHttpResponse(body={}),
            ("POST", f"{fields_url}/mmu_gate_map"): DummyHttpResponse(body={}),
        })
        self.server._components["http_client"] = http_client

        result = await controller._ensure_spoolman_extras()

        self.assertTrue(result)
        self.assertTrue(controller._spoolman_extras_ready)
        posted_keys = {url.rsplit("/", 1)[-1] for method, url, _ in http_client.requests if method == "POST"}
        self.assertEqual(posted_keys, {"printer_name", "mmu_gate_map"})

    async def test_skips_field_creation_when_already_present(self):
        controller = self._make_controller()
        info_url, fields_url = self._urls()
        http_client = DummyHttpClient(responses={
            ("GET", info_url): DummyHttpResponse(body={"version": "0.19.0"}),
            ("GET", fields_url): DummyHttpResponse(body=[{"key": "printer_name"}, {"key": "mmu_gate_map"}]),
        })
        self.server._components["http_client"] = http_client

        result = await controller._ensure_spoolman_extras()

        self.assertTrue(result)
        self.assertEqual([r for r in http_client.requests if r[0] == "POST"], [])

    async def test_is_idempotent_once_ready(self):
        controller = self._make_controller()
        info_url, fields_url = self._urls()
        http_client = DummyHttpClient(responses={
            ("GET", info_url): DummyHttpResponse(body={"version": "0.19.0"}),
            ("GET", fields_url): DummyHttpResponse(body=[{"key": "printer_name"}, {"key": "mmu_gate_map"}]),
        })
        self.server._components["http_client"] = http_client
        await controller._ensure_spoolman_extras()
        request_count = len(http_client.requests)

        result = await controller._ensure_spoolman_extras()

        self.assertTrue(result)
        self.assertEqual(len(http_client.requests), request_count)  # no new requests

    async def test_rejects_spoolman_version_older_than_minimum(self):
        controller = self._make_controller()
        info_url, fields_url = self._urls()
        http_client = DummyHttpClient(responses={
            ("GET", info_url): DummyHttpResponse(body={"version": "0.17.0"}),
        })
        self.server._components["http_client"] = http_client

        result = await controller._ensure_spoolman_extras()

        self.assertFalse(result)
        self.assertFalse(controller._spoolman_extras_ready)
        self.assertEqual([r for r in http_client.requests if "field" in r[1]], [])


class MmuAcePrinterNameResolutionTests(unittest.IsolatedAsyncioTestCase):
    """printer_name resolution priority: explicit config > Fluidd's own instance
    name (Settings -> General, stored server-side) > OS hostname (last resort -
    stock Rinkhals images all share the generic, non-unique "Rockchip")."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_mmu_ace_module()

    def setUp(self):
        self.original_create_task = self.module.asyncio.create_task
        self.module.asyncio.create_task = lambda coro: DummyTask(coro)

    def tearDown(self):
        self.module.asyncio.create_task = self.original_create_task

    def _make_controller(self, server, printer_name=None):
        controller = self.module.MmuAceController(
            server, host=None, spoolman_support="off", printer_name=printer_name
        )
        controller.ace = self.module.MmuAce()
        controller._handle_status_update = lambda *args, **kwargs: None
        return controller

    async def test_prefers_explicit_config_value(self):
        server = DummyServer(hostname="Rockchip")
        server._components["database"] = DummyDatabase({("fluidd", "uiSettings.general.instanceName"): "Ignored Fluidd Name"})
        controller = self._make_controller(server, printer_name="Explicit Name")

        name = await controller._resolve_printer_name()

        self.assertEqual(name, "Explicit Name")

    async def test_falls_back_to_fluidd_instance_name(self):
        server = DummyServer(hostname="Rockchip")
        server._components["database"] = DummyDatabase({("fluidd", "uiSettings.general.instanceName"): "Kobra S1 Max"})
        controller = self._make_controller(server)

        name = await controller._resolve_printer_name()

        self.assertEqual(name, "Kobra S1 Max")

    async def test_falls_back_to_hostname_as_last_resort(self):
        server = DummyServer(hostname="my-unique-host")  # no config value, empty database
        controller = self._make_controller(server)

        name = await controller._resolve_printer_name()

        self.assertEqual(name, "my-unique-host")

    async def test_hostname_fallback_still_resolves_even_when_generic(self):
        server = DummyServer(hostname="Rockchip")
        controller = self._make_controller(server)

        name = await controller._resolve_printer_name()

        self.assertEqual(name, "Rockchip")


class MmuAcePartialUpdateTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_mmu_ace_module()

    def setUp(self):
        self.original_create_task = self.module.asyncio.create_task
        self.module.asyncio.create_task = lambda coro: DummyTask(coro)
        self.server = DummyServer()
        self.controller = self.module.MmuAceController(self.server, host=None)
        self.controller.ace = self.module.MmuAce()
        self.status_updates = []
        self.controller._handle_status_update = lambda *args, **kwargs: self.status_updates.append(kwargs)

    def tearDown(self):
        self.module.asyncio.create_task = self.original_create_task

    async def test_partial_current_filament_update_syncs_loaded_state(self):
        await self.controller._handle_mmu_ace_status_update(
            {"filament_hub": {"current_filament": "0-0"}},
            0.0,
        )

        self.assertEqual(self.controller.ace.loaded_gate, 0)
        self.assertEqual(self.controller.ace.gate, 0)
        self.assertEqual(self.controller.ace.tool, 0)
        self.assertEqual(self.controller.ace.filament.pos, self.module.FILAMENT_POS_LOADED)
        self.assertEqual(self.status_updates, [{"force": True}])

    async def test_partial_empty_current_filament_clears_loaded_state(self):
        self.controller.ace.loaded_gate = 1
        self.controller.ace.gate = 1
        self.controller.ace.tool = 1
        self.controller.ace.filament.pos = self.module.FILAMENT_POS_LOADED

        await self.controller._handle_mmu_ace_status_update(
            {"filament_hub": {"current_filament": ""}},
            0.0,
        )

        self.assertEqual(self.controller.ace.loaded_gate, self.module.TOOL_GATE_UNKNOWN)
        self.assertEqual(self.controller.ace.gate, self.module.TOOL_GATE_UNKNOWN)
        self.assertEqual(self.controller.ace.tool, self.module.TOOL_GATE_UNKNOWN)
        self.assertEqual(self.controller.ace.filament.pos, self.module.FILAMENT_POS_UNLOADED)
        self.assertEqual(self.status_updates, [{"force": True}])

    async def test_partial_empty_current_filament_preserves_unloaded_selection(self):
        self.controller.ace.loaded_gate = self.module.TOOL_GATE_UNKNOWN
        self.controller.ace.gate = 2
        self.controller.ace.tool = 2
        self.controller.ace.filament.pos = self.module.FILAMENT_POS_UNLOADED

        await self.controller._handle_mmu_ace_status_update(
            {"filament_hub": {"current_filament": ""}},
            0.0,
        )

        self.assertEqual(self.controller.ace.loaded_gate, self.module.TOOL_GATE_UNKNOWN)
        self.assertEqual(self.controller.ace.gate, 2)
        self.assertEqual(self.controller.ace.tool, 2)
        self.assertEqual(self.controller.ace.filament.pos, self.module.FILAMENT_POS_UNLOADED)
        self.assertEqual(self.status_updates, [{"force": True}])

    async def test_partial_update_without_current_filament_is_ignored(self):
        await self.controller._handle_mmu_ace_status_update(
            {"filament_hub": {"filament_present": 1}},
            0.0,
        )

        self.assertEqual(self.controller.ace.loaded_gate, self.module.TOOL_GATE_UNKNOWN)
        self.assertEqual(self.controller.ace.gate, self.module.TOOL_GATE_UNKNOWN)
        self.assertEqual(self.controller.ace.tool, self.module.TOOL_GATE_UNKNOWN)
        self.assertEqual(self.status_updates, [])


class MmuAceDisconnectReconnectTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_mmu_ace_module()

    def setUp(self):
        self.original_create_task = self.module.asyncio.create_task
        self.module.asyncio.create_task = lambda coro: DummyTask(coro)
        self.server = DummyServer()
        self.controller = self.module.MmuAceController(self.server, host=None)
        self.controller.ace = self.module.MmuAce()
        self.status_updates = []
        self.controller._handle_status_update = lambda *args, **kwargs: self.status_updates.append(kwargs)

    def tearDown(self):
        self.module.asyncio.create_task = self.original_create_task

    def _build_filament_hub(self, current_filament: str = ""):
        return {
            "current_filament": current_filament,
            "filament_hubs": [
                {
                    "id": 0,
                    "status": "ready",
                    "temp": 25,
                    "slots": [
                        {"index": 0, "status": "ready", "sku": "", "type": "PLA", "color": [255, 255, 255], "rfid": 1, "source": 2},
                        {"index": 1, "status": "empty", "sku": "", "type": "", "color": [0, 0, 0], "rfid": 1, "source": 3},
                        {"index": 2, "status": "empty", "sku": "", "type": "", "color": [0, 0, 0], "rfid": 1, "source": 3},
                        {"index": 3, "status": "empty", "sku": "", "type": "", "color": [0, 0, 0], "rfid": 1, "source": 3},
                    ],
                }
            ],
        }

    async def test_disconnect_disables_ace(self):
        self.controller.ace.enabled = True

        await self.controller._handle_mmu_ace_status_update(
            {"filament_hub": {"filament_hubs": None}},
            0.0,
        )

        self.assertFalse(self.controller.ace.enabled)
        self.assertEqual(self.status_updates, [{"force": True}])

    async def test_disconnect_when_already_disabled_is_idempotent(self):
        self.controller.ace.enabled = False

        await self.controller._handle_mmu_ace_status_update(
            {"filament_hub": {"filament_hubs": None}},
            0.0,
        )

        self.assertFalse(self.controller.ace.enabled)
        self.assertEqual(self.status_updates, [])

    def test_reconnect_reenables_ace_in_set_ace_status(self):
        self.controller.ace.enabled = False

        self.controller._set_ace_status(self._build_filament_hub())

        self.assertTrue(self.controller.ace.enabled)

    def test_set_ace_status_does_not_reenable_when_already_enabled(self):
        self.controller.ace.enabled = True

        self.controller._set_ace_status(self._build_filament_hub())

        self.assertTrue(self.controller.ace.enabled)

    def test_disable_ace_resets_gate_fingerprint(self):
        self.controller._last_gate_fingerprint = "gate0:PLA|gate1:ASA"

        self.controller._disable_ace("test disconnect")

        self.assertEqual(self.controller._last_gate_fingerprint, "")

    def test_first_status_after_reconnect_is_not_suppressed_by_dedup(self):
        # Simulate state just before reconnect: fingerprint has stale data
        # identical to what the ACE will report on reconnect
        self.controller._set_ace_status(self._build_filament_hub())
        self.status_updates.clear()

        # Cable pulled — fingerprint reset, ace disabled
        self.controller._disable_ace("cable disconnected")
        self.status_updates.clear()

        # Cable replugged — same gate data as before disconnect
        self.controller._set_ace_status(self._build_filament_hub())

        # Must push an update even though data is identical to pre-disconnect
        self.assertEqual(self.status_updates, [{"force": True}])


class MmuRecoverTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_mmu_ace_module()

    async def test_mmu_recover_rejects_unsupported_arguments(self):
        refresh_calls = []
        responses = []
        gate = types.SimpleNamespace(status=self.module.GATE_AVAILABLE, filament_name="PLA")
        patcher = self.module.MmuAcePatcher.__new__(self.module.MmuAcePatcher)
        patcher.ace_controller = types.SimpleNamespace(
            _handle_status_update=lambda **kwargs: refresh_calls.append(kwargs)
        )
        patcher.ace = types.SimpleNamespace(units=[types.SimpleNamespace(gates=[gate])])

        async def send_response(message):
            responses.append(message)

        patcher._send_gcode_response = send_response

        result = await self.module.MmuAcePatcher._on_gcode_mmu_recover(
            patcher,
            {"GATE": "1", "LOADED": "1", "TOOL": "1"},
            None,
        )

        self.assertIsNone(result)
        self.assertEqual(refresh_calls, [])
        self.assertEqual(len(responses), 1)
        self.assertIn("unsupported parameters", responses[0])

    async def test_mmu_recover_refreshes_without_arguments(self):
        refresh_calls = []
        responses = []
        gate_ready = types.SimpleNamespace(status=self.module.GATE_AVAILABLE, filament_name="PLA")
        gate_empty = types.SimpleNamespace(status=self.module.GATE_EMPTY, filament_name="")
        patcher = self.module.MmuAcePatcher.__new__(self.module.MmuAcePatcher)
        patcher.ace_controller = types.SimpleNamespace(
            _handle_status_update=lambda **kwargs: refresh_calls.append(kwargs)
        )
        patcher.ace = types.SimpleNamespace(units=[types.SimpleNamespace(gates=[gate_ready, gate_empty])])

        async def send_response(message):
            responses.append(message)

        patcher._send_gcode_response = send_response

        result = await self.module.MmuAcePatcher._on_gcode_mmu_recover(patcher, {}, None)

        self.assertIsNone(result)
        self.assertEqual(refresh_calls, [{"force": True}])
        self.assertEqual(len(responses), 1)
        self.assertIn("ACE status refreshed", responses[0])


if __name__ == "__main__":
    unittest.main()
