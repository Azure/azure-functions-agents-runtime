import assert from "node:assert/strict";
import { it } from "node:test";

import { ClientSecretCredential } from "@azure/identity";

it("acquires an Entra token through Azure Identity and the patched MSAL dependency", async () => {
  const tenant = "11111111-1111-4111-8111-111111111111";
  const authority = `https://login.microsoftonline.com/${tenant}`;
  const requests: string[] = [];
  const credential = new ClientSecretCredential(
    tenant,
    "22222222-2222-4222-8222-222222222222",
    "dummy-test-secret",
    {
      disableInstanceDiscovery: true,
      httpClient: {
        async sendRequest(request) {
          const url = new URL(request.url);
          url.search = "";
          requests.push(url.href);
          let body: Record<string, unknown>;
          if (request.url.includes("openid-configuration")) {
            body = {
              authorization_endpoint: `${authority}/oauth2/v2.0/authorize`,
              token_endpoint: `${authority}/oauth2/v2.0/token`,
              issuer: `${authority}/v2.0`,
              jwks_uri: `${authority}/discovery/v2.0/keys`,
            };
          } else {
            assert.equal(url.href, `${authority}/oauth2/v2.0/token`);
            assert.equal(request.method, "POST");
            const form = new URLSearchParams(String(request.body));
            assert.equal(form.get("grant_type"), "client_credentials");
            body = {
              token_type: "Bearer",
              expires_in: 3600,
              access_token: "dummy-test-token",
            };
          }
          return {
            request,
            status: 200,
            headers: request.headers,
            bodyAsText: JSON.stringify(body),
          };
        },
      },
    },
  );

  const token = await credential.getToken(
    "https://management.azure.com/.default",
  );

  assert.equal(token.token, "dummy-test-token");
  assert.ok(token.expiresOnTimestamp > Date.now());
  assert.ok(requests.includes(`${authority}/oauth2/v2.0/token`));
});
