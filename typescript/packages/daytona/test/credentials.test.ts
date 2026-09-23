import { credentialCases } from "@threads/adapter-testkit";
import { daytona } from "../src";

// Lane 09: the credential defaults to secret("DAYTONA_API_KEY") and is resolved at setup.

/** No network in these tests: a request is a failure the test would see. */
const offline = async (): Promise<Response> => {
  throw new Error("no network in credential tests");
};

credentialCases({
  factory: "daytona",
  env: "DAYTONA_API_KEY",
  slot: (apiKey) => ({
    sandbox: daytona({
      fetch: offline,
      ...(apiKey === undefined ? {} : { apiKey }),
    }),
  }),
});
