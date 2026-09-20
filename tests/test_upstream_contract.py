"""Optional checks against the official npm contract; see README.md."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.api import (
    RemnawaveApi,
    build_reality_config,
    configure_warp_routing,
)
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.state import StateStore


CONTRACT_PATH = os.environ.get("RWM_BACKEND_CONTRACT_PATH")
PROFILE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
INBOUND = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NODE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


@unittest.skipUnless(CONTRACT_PATH, "Set RWM_BACKEND_CONTRACT_PATH to the official npm package")
class UpstreamContractTests(unittest.TestCase):
    def test_manager_requests_match_panel_contract(self) -> None:
        api = RemnawaveApi("https://panel.example.test")
        requests = []

        def capture(contract, response, action):
            with mock.patch.object(api, "request", return_value={"response": response}) as request:
                action()
            request.assert_called_once()
            args, kwargs = request.call_args
            requests.append({
                "contract": contract,
                "method": args[0],
                "path": args[1],
                "data": kwargs["data"],
            })

        for registering, contract in ((True, "RegisterCommand"), (False, "LoginCommand")):
            with mock.patch.object(api, "auth_status", return_value={
                "isRegisterAllowed": registering, "isLoginAllowed": not registering,
            }):
                capture(contract, {"accessToken": "test-admin-jwt"},
                        lambda: api.register_or_login("admin", "A" + "a" * 22 + "1"))
        capture("CreateApiTokenCommand", {"token": "test-api-token"},
                lambda: api.create_subscription_token("test-admin-jwt"))
        config = build_reality_config("node.example.test", "VLESS_REALITY", "test-private-key")
        capture("CreateConfigProfileCommand", {"uuid": PROFILE, "inbounds": [{"uuid": INBOUND}]},
                lambda: api.create_config_profile("test-admin-jwt", "Reality", config))
        capture("CreateNodeCommand", {"uuid": NODE}, lambda: api.create_node(
            "test-admin-jwt", name="Node", address="node.example.test",
            profile_uuid=PROFILE, inbound_uuid=INBOUND,
        ))
        capture("CreateHostCommand", {"uuid": NODE}, lambda: api.create_host(
            "test-admin-jwt", remark="Host", address="node.example.test",
            profile_uuid=PROFILE, inbound_uuid=INBOUND,
        ))
        capture("UpdateInternalSquadCommand", {"uuid": NODE, "inbounds": [{"uuid": INBOUND}]},
                lambda: api.update_internal_squad_inbounds("test-admin-jwt", NODE, [INBOUND]))

        # Exercise the actual WARP add/remove path, including read-back, without a server.
        def profile_request(method, path, **kwargs):
            nonlocal config
            if method == "PATCH":
                requests.append({
                    "contract": "UpdateConfigProfileCommand", "method": method,
                    "path": path, "data": copy.deepcopy(kwargs["data"]),
                })
                config = copy.deepcopy(kwargs["data"]["config"])
            return {"response": {"config": copy.deepcopy(config)}}

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(RuntimePaths(Path(directory)))
            with mock.patch.object(api, "request", side_effect=profile_request):
                configure_warp_routing(api, "test-admin-jwt", store, PROFILE, ["example.test"])
                configure_warp_routing(api, "test-admin-jwt", store, PROFILE, [], remove=True)

        self.assertEqual(len(requests), 9)
        result = subprocess.run(
            ["node", "-e", r"""
const fs = require('node:fs');
const assert = require('node:assert/strict');
const contract = require(process.argv[1]);
assert.equal(require(process.argv[1] + '/package.json').version, '3.4.15');
const requests = JSON.parse(fs.readFileSync(0, 'utf8'));
for (const request of requests) {
    const command = contract[request.contract];
    assert.ok(command, request.contract);
    assert.equal(command.url.replace(/\/$/, ''), request.path, request.contract);
    assert.equal(command.endpointDetails.REQUEST_METHOD, request.method.toLowerCase());
    const parsed = command.RequestBodySchema.safeParse(request.data);
    assert.ok(parsed.success, request.contract + ': ' + JSON.stringify(parsed.error));
    // Zod silently strips unknown properties: reject such contract drift too.
    for (const key of Object.keys(request.data)) {
        assert.ok(key in parsed.data, request.contract + ': dropped field ' + key);
    }
}
const subscriptionEndpoints = [
    'GetMetadataCommand', 'GetUserByUsernameCommand',
    'GetSubpageConfigsCommand', 'GetSubpageConfigCommand',
    'GetSubscriptionByShortUuidProtectedCommand', 'GetSubpageConfigByShortUuidCommand',
];
const scopes = subscriptionEndpoints.map(name => {
    const command = contract[name];
    assert.equal(command.endpointDetails.SCOPE_KIND, 'read');
    const url = typeof command.url === 'function' ? command.url('probe') : command.url;
    return url.split('/')[2] + ':' + command.endpointDetails.SCOPE;
});
const tokenRequest = requests.find(request => request.contract === 'CreateApiTokenCommand');
assert.deepEqual([...tokenRequest.data.scopes].sort(), scopes.sort());
console.log(requests.length + ' manager requests validated against backend-contract 3.4.15');
""", str(Path(CONTRACT_PATH).resolve())],
            input=json.dumps(requests), text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
